"""Model provider interface (item 17).

Every model call in the app goes through a Provider so a specialist, the
extractor, the router, or the coordinator can run on a different model or
vendor by configuration. Each call returns the parsed result and a ModelCall
record with token counts and an estimated cost, which board runs log.

Providers:
  OpenAIProvider  uses chat.completions.parse with a pydantic schema
                  (structured outputs). Reads OPENAI_API_KEY, or the Replit
                  integration variables, and an optional OPENAI_BASE_URL.
  FakeProvider    canned responses for tests and local runs without a key.

Anthropic is not implemented yet: the owner has no key in place. Add
AnthropicProvider here when it arrives; nothing else needs to change.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens (input, output). Approximate list prices; edit as they change.
# Unknown models get cost None, never a guess.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "claude-sonnet-5-5": (3.00, 15.00),
    "claude-opus-5-5": (15.00, 75.00),
    "claude-haiku-5-5": (0.80, 4.00),
}

DEFAULT_MODELS = {
    # Owner's decision (HANDOFF 4.6) once an Anthropic key exists:
    # extraction and specialists on Sonnet, coordinator on Opus, routing on Haiku.
    # Until then everything runs on OpenAI.
    "extraction": os.environ.get("MODEL_EXTRACTION", "gpt-4o"),
    "specialist": os.environ.get("MODEL_SPECIALIST", "gpt-4o"),
    "coordinator": os.environ.get("MODEL_COORDINATOR", "gpt-4o"),
    "router": os.environ.get("MODEL_ROUTER", "gpt-4o-mini"),
    "translate": os.environ.get("MODEL_TRANSLATE", "gpt-4o-mini"),
    # Speech to text. The diarizing model labels speakers (A, B, ...); the plain
    # model is the fallback when the endpoint or account does not offer it.
    "transcription": os.environ.get("MODEL_TRANSCRIPTION", "gpt-4o-transcribe-diarize"),
    "transcription_fallback": os.environ.get("MODEL_TRANSCRIPTION_FALLBACK", "gpt-4o-transcribe"),
    "summary": os.environ.get("MODEL_SUMMARY", "gpt-4o"),
}

# USD per minute of audio. Unknown models get cost None, never a guess.
PRICES_PER_MINUTE: dict[str, float] = {
    "whisper-1": 0.006,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
}


def estimate_audio_cost(model: str, seconds: float | None) -> float | None:
    price = PRICES_PER_MINUTE.get(model)
    if price is None or not seconds:
        return None
    return round(seconds / 60 * price, 6)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    return round((input_tokens * price[0] + output_tokens * price[1]) / 1_000_000, 6)


@dataclass
class ModelCall:
    provider: str
    model: str
    purpose: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    duration_ms: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider, "model": self.model, "purpose": self.purpose,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd, "duration_ms": self.duration_ms, "error": self.error,
        }


@dataclass
class Part:
    """One piece of user content: text or a PNG image."""
    text: str | None = None
    image_png: bytes | None = None

    @staticmethod
    def of_text(text: str) -> "Part":
        return Part(text=text)

    @staticmethod
    def of_png(data: bytes) -> "Part":
        return Part(image_png=data)


@dataclass
class Transcript:
    """Speech to text result. segments carry speaker labels when the model
    diarized (A, B, ...); otherwise there is one segment with speaker None."""
    text: str
    segments: list[dict[str, Any]] = field(default_factory=list)
    duration_seconds: float | None = None
    model: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "segments": self.segments, "duration_seconds": self.duration_seconds, "model": self.model}


class Provider(Protocol):
    name: str

    def transcribe(self, *, data: bytes, filename: str, purpose: str, language: str | None = None,
                   models: list[str] | None = None) -> tuple["Transcript", ModelCall]: ...

    def structured(self, *, model: str, system: str, parts: list[Part], schema: type[T], purpose: str,
                   max_tokens: int = 4000, temperature: float = 0.0) -> tuple[T, ModelCall]: ...

    def text(self, *, model: str, system: str, parts: list[Part], purpose: str,
             max_tokens: int = 2000, temperature: float = 0.0) -> tuple[str, ModelCall]: ...


class ProviderError(Exception):
    pass


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        import openai

        key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set")
        url = base_url or os.environ.get("OPENAI_BASE_URL") or os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
        self._client = openai.OpenAI(api_key=key, base_url=url) if url else openai.OpenAI(api_key=key)

    def _parse_fn(self):
        """Structured outputs moved from client.beta.chat.completions.parse (SDK 1.x)
        to client.chat.completions.parse (SDK 2+). Support both."""
        fn = getattr(self._client.chat.completions, "parse", None)
        if fn is None:
            fn = self._client.beta.chat.completions.parse
        return fn

    @staticmethod
    def _content(parts: list[Part]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in parts:
            if p.text is not None:
                out.append({"type": "text", "text": p.text})
            elif p.image_png is not None:
                b64 = base64.b64encode(p.image_png).decode("ascii")
                mime = "image/jpeg" if p.image_png[:3] == b"\xff\xd8\xff" else "image/png"
                out.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"}})
        return out

    def structured(self, *, model, system, parts, schema, purpose, max_tokens=4000, temperature=0.0):
        call = ModelCall(self.name, model, purpose)
        t0 = time.time()
        try:
            completion = self._parse_fn()(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": self._content(parts)}],
                response_format=schema,
                max_completion_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:  # network, auth, refusal, schema
            call.error = f"{type(e).__name__}: {e}"
            call.duration_ms = int((time.time() - t0) * 1000)
            raise ProviderError(call.error) from e
        call.duration_ms = int((time.time() - t0) * 1000)
        usage = getattr(completion, "usage", None)
        if usage is not None:
            call.input_tokens = usage.prompt_tokens or 0
            call.output_tokens = usage.completion_tokens or 0
            call.cost_usd = estimate_cost(model, call.input_tokens, call.output_tokens)
        message = completion.choices[0].message
        if getattr(message, "refusal", None):
            call.error = f"refusal: {message.refusal}"
            raise ProviderError(call.error)
        if message.parsed is None:
            call.error = "no parsed output"
            raise ProviderError(call.error)
        return message.parsed, call

    def text(self, *, model, system, parts, purpose, max_tokens=2000, temperature=0.0):
        call = ModelCall(self.name, model, purpose)
        t0 = time.time()
        try:
            completion = self._client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": self._content(parts)}],
                max_completion_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:
            call.error = f"{type(e).__name__}: {e}"
            call.duration_ms = int((time.time() - t0) * 1000)
            raise ProviderError(call.error) from e
        call.duration_ms = int((time.time() - t0) * 1000)
        usage = getattr(completion, "usage", None)
        if usage is not None:
            call.input_tokens = usage.prompt_tokens or 0
            call.output_tokens = usage.completion_tokens or 0
            call.cost_usd = estimate_cost(model, call.input_tokens, call.output_tokens)
        return completion.choices[0].message.content or "", call

    def transcribe(self, *, data, filename, purpose, language=None, models=None):
        """Try each model in turn: a diarizing model first, then plain transcription.
        Different SDK versions and gateways accept different arguments, so an
        argument the SDK rejects just moves on to the next attempt."""
        models = models or [DEFAULT_MODELS["transcription"], DEFAULT_MODELS["transcription_fallback"], "whisper-1"]
        errors: list[str] = []
        t0 = time.time()
        for model in models:
            call = ModelCall(self.name, model, purpose)
            kwargs: dict[str, Any] = {"model": model, "file": (filename, data)}
            if language:
                kwargs["language"] = language
            if "diarize" in model:
                kwargs["response_format"] = "diarized_json"
                kwargs["chunking_strategy"] = "auto"
            else:
                kwargs["response_format"] = "json"
            try:
                result = self._client.audio.transcriptions.create(**kwargs)
            except Exception as e:
                errors.append(f"{model}: {type(e).__name__}: {e}")
                continue
            call.duration_ms = int((time.time() - t0) * 1000)
            text = getattr(result, "text", None) or (result if isinstance(result, str) else "")
            segments = []
            for seg in getattr(result, "segments", None) or []:
                segments.append({"speaker": getattr(seg, "speaker", None), "start": getattr(seg, "start", None),
                                 "end": getattr(seg, "end", None), "text": getattr(seg, "text", "")})
            duration = getattr(result, "duration", None)
            usage = getattr(result, "usage", None)
            if duration is None and usage is not None:
                duration = getattr(usage, "seconds", None)
            if usage is not None and getattr(usage, "input_tokens", None) is not None:
                call.input_tokens = usage.input_tokens or 0
                call.output_tokens = getattr(usage, "output_tokens", 0) or 0
            call.cost_usd = estimate_audio_cost(model, duration)
            if not segments:
                segments = [{"speaker": None, "start": 0.0, "end": duration, "text": text}]
            return Transcript(text=text or "", segments=segments, duration_seconds=duration, model=model), call
        msg = "transcription failed: " + " | ".join(errors)
        raise ProviderError(msg)


@dataclass
class FakeProvider:
    """Returns canned results. responses maps a schema class (or its name) to an
    instance, a dict, or a callable(parts) -> instance/dict. texts maps purpose
    to a string or callable(parts) -> str. Every call is recorded in calls."""
    name: str = "fake"
    responses: dict[Any, Any] = field(default_factory=dict)
    texts: dict[str, Any] = field(default_factory=dict)
    transcripts: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 100
    output_tokens: int = 50

    def _record(self, model: str, purpose: str, system: str, parts: list[Part], schema: Any) -> ModelCall:
        self.calls.append({"model": model, "purpose": purpose, "system": system, "parts": parts,
                           "schema": getattr(schema, "__name__", schema)})
        return ModelCall(self.name, model, purpose, self.input_tokens, self.output_tokens,
                         estimate_cost(model, self.input_tokens, self.output_tokens), 1)

    def structured(self, *, model, system, parts, schema, purpose, max_tokens=4000, temperature=0.0):
        call = self._record(model, purpose, system, parts, schema)
        canned = self.responses.get(schema, self.responses.get(schema.__name__))
        if canned is None:
            raise ProviderError(f"FakeProvider has no response for {schema.__name__}")
        if callable(canned) and not isinstance(canned, BaseModel):
            canned = canned(parts)
        if isinstance(canned, dict):
            canned = schema.model_validate(canned)
        return canned, call

    def text(self, *, model, system, parts, purpose, max_tokens=2000, temperature=0.0):
        call = self._record(model, purpose, system, parts, None)
        canned = self.texts.get(purpose, self.texts.get("*", ""))
        if callable(canned):
            canned = canned(parts)
        return str(canned), call

    def transcribe(self, *, data, filename, purpose, language=None, models=None):
        self.calls.append({"model": "fake-transcribe", "purpose": purpose, "filename": filename, "bytes": len(data)})
        canned = self.transcripts.get(purpose, self.transcripts.get("*"))
        if canned is None:
            raise ProviderError(f"FakeProvider has no transcript for {purpose}")
        if callable(canned):
            canned = canned(data, filename)
        if isinstance(canned, str):
            canned = Transcript(text=canned, segments=[{"speaker": None, "start": 0.0, "end": None, "text": canned}],
                                duration_seconds=None, model="fake-transcribe")
        elif isinstance(canned, dict):
            canned = Transcript(**canned)
        return canned, ModelCall(self.name, "fake-transcribe", purpose, 0, 0, None, 1)


_provider_factories: dict[str, Callable[[], Provider]] = {
    "openai": OpenAIProvider,
}


def get_provider(name: str | None = None) -> Provider:
    """Provider by name, default from MODEL_PROVIDER (openai). 'fake' is for tests."""
    name = (name or os.environ.get("MODEL_PROVIDER") or "openai").lower()
    if name == "fake":
        return FakeProvider()
    if name == "demo":
        from demo import DemoProvider
        return DemoProvider()
    factory = _provider_factories.get(name)
    if factory is None:
        raise ProviderError(f"unknown provider {name}; known: {sorted(_provider_factories)}")
    return factory()


def provider_available(name: str | None = None) -> bool:
    try:
        get_provider(name)
        return True
    except ProviderError:
        return False
