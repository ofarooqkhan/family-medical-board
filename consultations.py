"""Doctor conversations: an audio recording of a visit becomes a transcript,
a plain-language summary, the actions the doctor asked for, and the
medication changes that were said out loud. Everything is stored as its own
record (consultations table) next to the audio file, and the medication and
follow-up items go through the same pending-review step as a scanned
prescription, so nothing enters the record without a person confirming it.

The summary reports what was said. It never adds advice of its own.
"""
from __future__ import annotations

import datetime as _dt
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel

import database as db
from database import _assert_patient, audit, db_cursor, require_role
from providers import DEFAULT_MODELS, Part, Provider, ProviderError, Transcript
from utils import json_dumps_safe

AUDIO_TYPES = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/webm": "webm", "audio/ogg": "ogg",
    "audio/flac": "flac", "audio/aac": "aac", "video/mp4": "mp4", "video/webm": "webm",
}
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # the transcription endpoint's limit per file


# ---------------------------------------------------------------------------
# Schema the summary model must return
# ---------------------------------------------------------------------------

class Speaker(BaseModel):
    label: str                       # A, B, ... or the name used in the transcript
    role: Literal["doctor", "patient", "family", "nurse", "other", "unknown"]
    name: Optional[str] = None
    confidence: float


class Turn(BaseModel):
    speaker: str                     # label from speakers
    text: str


class Action(BaseModel):
    text: str                        # what was asked for, in the doctor's words where possible
    who: Literal["family", "patient", "doctor", "pharmacy", "lab", "other"]
    when: Optional[str] = None       # as said: "before next visit", "in two weeks", a date
    kind: Literal["test", "appointment", "medication", "lifestyle", "monitoring", "referral", "other"]
    confidence: float


class MedicationChange(BaseModel):
    name: str
    generic_name: Optional[str] = None
    dose: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    change: Literal["start", "stop", "change", "continue", "unclear"]
    reason: Optional[str] = None
    instructions: Optional[str] = None
    confidence: float
    low_confidence_fields: list[str]


class FollowUp(BaseModel):
    title: str
    when_text: Optional[str] = None   # as said
    date: Optional[str] = None        # ISO date if one can be fixed from the conversation
    doctor_name: Optional[str] = None
    specialty: Optional[str] = None
    confidence: float


class ConsultationSummary(BaseModel):
    title: str                       # short, e.g. "Cardiology review with Dr. Lee"
    language: Optional[str] = None
    happened_on: Optional[str] = None  # ISO date if said
    doctor_name: Optional[str] = None
    doctor_specialty: Optional[str] = None
    doctor_confidence: float
    speakers: list[Speaker]
    turns: list[Turn]                # the transcript split by speaker
    context: str                     # why the visit happened, in one or two sentences
    summary: str                     # what was discussed and decided, plain language
    findings_discussed: list[str]
    decisions: list[str]
    actions: list[Action]
    medication_changes: list[MedicationChange]
    follow_ups: list[FollowUp]
    questions_left_open: list[str]
    overall_confidence: float


SUMMARY_SYSTEM = (
    "You read the transcript of a conversation between a doctor and a patient or the patient's family. "
    "Your job is to record what was said, not to add to it. "
    "Work out who the doctor is from how people speak (who examines, explains results, prescribes, orders tests) "
    "and give that a confidence. If the transcript carries speaker labels (A, B, ...), keep them; otherwise split "
    "the text into turns yourself and label the speakers A, B, C. "
    "Report the context of the visit, a plain summary of what was discussed, the findings mentioned, the decisions "
    "made, the actions asked of the family or patient, the medication changes said out loud (start, stop, change, "
    "continue), any follow-up visits or tests with timing as said, and questions that were raised but not answered. "
    "Quote doses and frequencies exactly as spoken; put a field in low_confidence_fields when the audio was unclear. "
    "Never add medical advice, interpretation, or recommendations that were not spoken in the conversation. "
    "Write in the language of the conversation; if it mixes Urdu and English, write the summary in English. "
    "When the patient's name is given, use their first name instead of 'the patient'."
)


def first_name(name: str | None) -> str:
    return (name or "").strip().split(" ")[0] if name else ""


def summarize_transcript(provider: Provider, model: str, transcript: Transcript,
                         patient_name: str | None = None) -> tuple[ConsultationSummary, Any]:
    lines = []
    if patient_name:
        first = first_name(patient_name)
        lines.append(f"Patient on record: {patient_name}. In the summary, context, decisions, actions and turns, call them "
                     f"{first}, never 'the patient'. If the conversation is clearly about someone else, say so in the summary.")
    if transcript.segments and any(s.get("speaker") for s in transcript.segments):
        lines.append("Transcript with speaker labels:")
        for s in transcript.segments:
            lines.append(f"[{s.get('speaker') or '?'}] {s.get('text', '')}")
    else:
        lines.append("Transcript (no speaker labels):")
        lines.append(transcript.text)
    return provider.structured(model=model, system=SUMMARY_SYSTEM, parts=[Part.of_text("\n".join(lines))],
                               schema=ConsultationSummary, purpose="consultation_summary", max_tokens=6000)


# ---------------------------------------------------------------------------
# Items for the pending-review queue
# ---------------------------------------------------------------------------

def items_from_summary(summary: ConsultationSummary, base_date: _dt.date | None = None) -> list[dict]:
    """Medication changes become pending medication items; follow-ups with a
    fixed date become pending appointment items. Stops and continues are kept
    on the consultation record only (the medication list is edited by hand)."""
    items: list[dict] = []
    prescriber = summary.doctor_name
    for m in summary.medication_changes:
        if m.change not in ("start", "change", "unclear"):
            continue
        payload = {
            "name": m.name, "generic_name": m.generic_name, "dose": m.dose, "route": m.route, "frequency": m.frequency,
            "duration": None, "instructions": " ".join(x for x in (m.instructions, f"({m.change} as said in the conversation)") if x),
            "prescriber": prescriber, "start_date": summary.happened_on or (base_date.isoformat() if base_date else None),
            "page": 1, "confidence": m.confidence, "low_confidence_fields": m.low_confidence_fields,
            "change": m.change, "reason": m.reason,
        }
        items.append({"page_number": 1, "item_type": "medication", "payload": payload, "confidence": m.confidence})
    for f in summary.follow_ups:
        if not f.date:
            continue
        payload = {"title": f.title, "starts_at": f.date, "doctor_name": f.doctor_name or prescriber,
                   "specialty": f.specialty or summary.doctor_specialty, "notes": f.when_text,
                   "confidence": f.confidence, "low_confidence_fields": []}
        items.append({"page_number": 1, "item_type": "appointment", "payload": payload, "confidence": f.confidence})
    return items


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def ingest_audio(patient_id: int, user: dict, filename: str, mime_type: str, data: bytes, provider: Provider | None,
                 language: str | None = None, happened_on: _dt.date | None = None) -> dict[str, Any]:
    """Store the recording, transcribe, summarize, queue items. Returns {consultation_id, file_id, items, error}."""
    require_role(user, "editor")
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError("The recording is larger than 25 MB. Split it or compress it before uploading.")
    patient = db.get_patient(patient_id, user)
    file_id = db.save_document(patient_id, user, filename, mime_type, data, document_kind="consultation_audio")
    with db_cursor(commit=True) as cur:
        _assert_patient(cur, patient_id)
        cur.execute("""INSERT INTO consultations (patient_id, file_id, title, happened_on, status, created_by)
                       VALUES (%s, %s, %s, %s, 'processing', %s) RETURNING id""",
                    (patient_id, file_id, f"Conversation {filename}", happened_on, user["id"]))
        cid = cur.fetchone()["id"]
        audit(cur, user, patient_id, "consultation.add", "consultation", cid, {"filename": filename, "bytes": len(data)})
    out: dict[str, Any] = {"consultation_id": cid, "file_id": file_id, "items": 0, "error": None, "calls": []}
    if provider is None:
        _set_error(cid, patient_id, user, "No model key configured: the recording is stored but not transcribed.")
        out["error"] = "no model"
        return out
    return process(cid, patient_id, user, provider, language=language, patient_name=(patient or {}).get("name"), out=out)


def process(cid: int, patient_id: int, user: dict, provider: Provider, language: str | None = None,
            patient_name: str | None = None, out: dict | None = None) -> dict[str, Any]:
    """Transcribe and summarize a stored consultation (also used to retry)."""
    require_role(user, "editor")
    out = out or {"consultation_id": cid, "items": 0, "error": None, "calls": []}
    row = get_consultation(cid, patient_id, user)
    if row is None:
        raise PermissionError(f"no consultation {cid} for patient {patient_id}")
    doc = db.get_document(row["file_id"], patient_id, user, with_data=True) if row.get("file_id") else None
    if doc is None:
        _set_error(cid, patient_id, user, "The audio file is missing.")
        out["error"] = "no audio"
        return out
    t0 = time.time()
    try:
        transcript, call = provider.transcribe(data=bytes(doc["file_data"]), filename=doc["filename"],
                                               purpose="consultation_transcribe", language=language)
        out["calls"].append(call.as_dict())
        summary, call2 = summarize_transcript(provider, DEFAULT_MODELS["summary"], transcript, patient_name)
        out["calls"].append(call2.as_dict())
    except ProviderError as e:
        _set_error(cid, patient_id, user, str(e))
        out["error"] = str(e)
        return out
    cost = sum((c.get("cost_usd") or 0) for c in out["calls"]) or None
    happened = _parse_date(summary.happened_on) or row.get("happened_on") or (doc.get("uploaded_at") or _dt.datetime.now()).date()
    with db_cursor(commit=True) as cur:
        cur.execute("""UPDATE consultations SET title = %s, happened_on = %s, doctor_name = %s, specialty = %s, transcript = %s,
                       segments = %s, summary = %s, status = 'ready', error = NULL, transcription_model = %s, summary_model = %s,
                       cost_usd = %s, duration_seconds = %s, updated_at = CURRENT_TIMESTAMP
                       WHERE id = %s AND patient_id = %s""",
                    (summary.title[:255], happened, summary.doctor_name, summary.doctor_specialty, transcript.text,
                     json_dumps_safe(transcript.segments), json_dumps_safe(summary.model_dump()), transcript.model,
                     DEFAULT_MODELS["summary"], cost, transcript.duration_seconds, cid, patient_id))
        audit(cur, user, patient_id, "consultation.summarize", "consultation", cid,
              {"doctor": summary.doctor_name, "actions": len(summary.actions), "medication_changes": len(summary.medication_changes)})
    # Standing instructions and the doctor go into the shared profile; the visit becomes a general duty note.
    import memory
    memory.record_consultation(patient_id, user, summary.model_dump(), cid, row["file_id"], happened)
    db.set_document_extraction(row["file_id"], patient_id, user, extraction={"consultation_id": cid, "calls": out["calls"]},
                               model=transcript.model, document_kind="consultation_audio", document_date=happened,
                               extracted_text=transcript.text)
    _discard_pending(patient_id, row["file_id"], user)
    items = items_from_summary(summary, happened)
    ids = db.add_extracted_items(patient_id, row["file_id"], user, items) if items else []
    out.update({"items": len(ids), "item_ids": ids, "duration_ms": int((time.time() - t0) * 1000), "summary": summary.model_dump(),
                "added": [], "to_check": []})
    import documents
    if documents.auto_add_enabled() and ids:
        out.update(documents.auto_confirm(patient_id, row["file_id"], user, ids))
    elif ids:
        out["to_check"] = [documents.item_label(i) for i in items]
    return out


def _discard_pending(patient_id: int, file_id: int, user: dict) -> None:
    for it in db.list_extracted_items(patient_id, user, file_id=file_id, status="pending"):
        db.set_extracted_item_status(it["id"], patient_id, user, "discarded")


def _set_error(cid: int, patient_id: int, user: dict, message: str) -> None:
    with db_cursor(commit=True) as cur:
        cur.execute("UPDATE consultations SET status = 'error', error = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND patient_id = %s",
                    (message[:2000], cid, patient_id))
        audit(cur, user, patient_id, "consultation.error", "consultation", cid, {"error": message[:300]})


def _parse_date(value: Any) -> _dt.date | None:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Reads and edits
# ---------------------------------------------------------------------------

def list_consultations(patient_id: int, user: dict, limit: int = 100):
    require_role(user, "viewer")
    with db_cursor() as cur:
        cur.execute("""SELECT id, patient_id, file_id, title, happened_on, doctor_name, specialty, status, error, summary,
                              transcription_model, summary_model, cost_usd, duration_seconds, created_by, created_at
                       FROM consultations WHERE patient_id = %s ORDER BY happened_on DESC NULLS LAST, id DESC LIMIT %s""",
                    (patient_id, limit))
        return cur.fetchall()


def get_consultation(cid: int, patient_id: int, user: dict):
    require_role(user, "viewer")
    with db_cursor() as cur:
        cur.execute("SELECT * FROM consultations WHERE id = %s AND patient_id = %s", (cid, patient_id))
        return cur.fetchone()


EDITABLE = ("title", "happened_on", "doctor_name", "specialty")


def update_consultation(cid: int, patient_id: int, user: dict, **fields) -> None:
    require_role(user, "editor")
    clean = {k: v for k, v in fields.items() if k in EDITABLE}
    if not clean:
        return
    with db_cursor(commit=True) as cur:
        sets = ", ".join(f"{k} = %s" for k in clean)
        cur.execute(f"UPDATE consultations SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND patient_id = %s",
                    (*clean.values(), cid, patient_id))
        if cur.rowcount != 1:
            raise PermissionError(f"no consultation {cid} for patient {patient_id}")
        audit(cur, user, patient_id, "consultation.update", "consultation", cid,
              {k: (v.isoformat() if isinstance(v, _dt.date) else v) for k, v in clean.items()})


def delete_consultation(cid: int, patient_id: int, user: dict) -> None:
    """Removes the record and its audio file (and the file's pending items)."""
    require_role(user, "editor")
    row = get_consultation(cid, patient_id, user)
    if row is None:
        raise PermissionError(f"no consultation {cid} for patient {patient_id}")
    with db_cursor(commit=True) as cur:
        cur.execute("DELETE FROM consultations WHERE id = %s AND patient_id = %s", (cid, patient_id))
        audit(cur, user, patient_id, "consultation.delete", "consultation", cid, {"title": row["title"]})
    if row.get("file_id"):
        db.delete_document(row["file_id"], patient_id, user)


def recent_for_brief(patient_id: int, user: dict, since: _dt.datetime, limit: int = 5) -> list[dict]:
    """Ready consultations in the window, trimmed for the pre-visit brief."""
    out = []
    for c in list_consultations(patient_id, user, limit=50):
        if c["status"] != "ready" or not c.get("summary"):
            continue
        when = c.get("happened_on")
        if when and _dt.datetime.combine(when, _dt.time()) < since:
            continue
        s = c["summary"] if isinstance(c["summary"], dict) else {}
        out.append({"id": c["id"], "title": c["title"], "date": when.isoformat() if when else "", "doctor": c.get("doctor_name"),
                    "summary": s.get("summary", ""), "actions": [a.get("text") for a in s.get("actions", [])],
                    "open_questions": s.get("questions_left_open", [])})
        if len(out) >= limit:
            break
    return out
