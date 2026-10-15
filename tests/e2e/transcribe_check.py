"""Live check for doctor conversations: make a short spoken clip with the
configured OpenAI account (text to speech), transcribe it through the
provider, summarize it, print the result. No database rows are written.

Run from the repo root with the OpenAI variables set:
  python tests/e2e/transcribe_check.py            # prints to stdout
  python tests/e2e/transcribe_check.py out.txt    # also writes the report
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import consultations
from providers import DEFAULT_MODELS, OpenAIProvider, ProviderError

SCRIPT = (
    "Doctor: Good morning. I looked at the blood pressure readings you brought. They are still high, around one fifty over ninety. "
    "Family member: We measure it every morning as you asked. "
    "Doctor: Good. I will start amlodipine five milligrams once daily in the morning, and keep the metformin as it is. "
    "Please repeat the kidney test and the potassium in four weeks and come back to see me with the results. "
    "Family member: Should we stop the aspirin? "
    "Doctor: No, continue the aspirin seventy five milligrams. We will review it after the test results."
)


def main() -> int:
    out = []

    def p(s=""):
        print(s)
        out.append(str(s))

    t0 = time.time()
    provider = OpenAIProvider()
    client = provider._client
    p("step: text to speech")
    try:
        speech = client.audio.speech.create(model="gpt-4o-mini-tts", voice="alloy", input=SCRIPT, response_format="mp3")
        audio = speech.content if hasattr(speech, "content") else speech.read()
    except Exception as e:
        p(f"tts failed: {type(e).__name__}: {e}")
        audio = None
    if not audio:
        clip = os.environ.get("TRANSCRIBE_CHECK_AUDIO")
        if clip and os.path.exists(clip):
            with open(clip, "rb") as fh:
                audio = fh.read()
            p(f"using the clip at {clip}")
        else:
            # No speech available: a two second tone still shows whether the
            # transcription endpoint is reachable (the text will be empty or noise).
            import io
            import math
            import struct
            import wave
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(32000)))
            audio = buf.getvalue()
            p("no speech available, sending a two second tone to test the endpoint only")
    p(f"audio bytes {len(audio)}")

    p("step: transcribe (models tried in order: "
      + ", ".join([DEFAULT_MODELS["transcription"], DEFAULT_MODELS["transcription_fallback"], "whisper-1"]) + ")")
    try:
        transcript, call = provider.transcribe(data=audio, filename="livecheck.wav" if audio[:4] == b"RIFF" else "livecheck.mp3", purpose="consultation_transcribe")
    except ProviderError as e:
        p(f"transcription failed: {e}")
        return 1
    p(f"model used {transcript.model}, duration {transcript.duration_seconds}, cost {call.cost_usd}, {call.duration_ms} ms")
    labelled = [s for s in transcript.segments if s.get("speaker")]
    p(f"segments {len(transcript.segments)}, with speaker labels {len(labelled)}")
    p("transcript: " + transcript.text[:600])

    p("step: summarize")
    try:
        summary, call2 = consultations.summarize_transcript(provider, DEFAULT_MODELS["summary"], transcript, "Live Check Patient")
    except ProviderError as e:
        p(f"summary failed: {e}")
        return 1
    p(f"summary model {call2.model}, tokens {call2.input_tokens}/{call2.output_tokens}, cost {call2.cost_usd}")
    p(json.dumps({
        "title": summary.title, "doctor": summary.doctor_name, "specialty": summary.doctor_specialty,
        "doctor_confidence": summary.doctor_confidence,
        "speakers": [s.model_dump() for s in summary.speakers],
        "summary": summary.summary, "decisions": summary.decisions,
        "actions": [a.model_dump() for a in summary.actions],
        "medication_changes": [m.model_dump() for m in summary.medication_changes],
        "follow_ups": [f.model_dump() for f in summary.follow_ups],
    }, indent=1, ensure_ascii=False))
    items = consultations.items_from_summary(summary)
    p(f"pending items that would be queued: {[(i['item_type'], i['payload'].get('name') or i['payload'].get('title')) for i in items]}")
    p(f"ALL STEPS OK in {time.time() - t0:.0f}s")
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
