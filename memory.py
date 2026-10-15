"""Persistent memory for the specialists, built so the board gets better
with use instead of only accumulating notes.

Three layers, all in the doctor_memory table and all editable on the
Memory page:

  Profile (doctor_type "shared", per patient): stable facts the family and
    the records have established (diagnoses, allergies, baselines, who the
    doctors are) and standing instructions doctors gave in conversations.
    Every specialist reads the whole active profile. After each board run a
    small model call merges what the specialists found into the profile,
    supersedes facts that a newer one replaces (the old row is kept and can
    be restored), and lists contradictions for the family.

  Notes (doctor_type <agent>, per patient): each specialist's own episodic
    observations from runs, documents and conversations. A specialist sees
    only its own notes, chosen by relevance to the question, then by
    importance with a slow decay over months.

  Lessons (doctor_type "family", no patient): whenever the family corrects or
    removes a note, the correction is stored as a lesson every specialist
    reads for every patient, so the same mistake is not repeated.

Nothing here gives advice; the model calls only organize what the records
and the family have already said.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel

import database as db
from providers import DEFAULT_MODELS, ModelCall, Part, Provider, ProviderError

PROFILE_CATEGORIES = ("diagnosis", "allergy", "baseline", "doctor", "preference", "instruction", "other")
MAX_PROFILE_FACTS = 60
MAX_NOTES_PER_AGENT = 8
MAX_LESSONS = 10
_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9]{2,}")
_STOP = {"the", "and", "for", "with", "what", "does", "about", "have", "has", "his", "her", "their", "from", "this", "that",
         "show", "records", "record", "latest", "please", "how", "are", "was", "were", "any", "can", "you"}


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP}


def _months_old(row: dict, now: _dt.datetime | None = None) -> float:
    created = row.get("created_at")
    if not isinstance(created, _dt.datetime):
        return 0.0
    now = now or _dt.datetime.now()
    return max(0.0, (now - created).days / 30.0)


def score(row: dict, query_words: set[str], now: _dt.datetime | None = None) -> float:
    """Relevance to the question first, then importance with a slow decay
    (one point per two months, never below one)."""
    overlap = len(query_words & _words(row.get("content") or "")) if query_words else 0
    importance = float(row.get("importance") or 5)
    decayed = max(1.0, importance - _months_old(row, now) / 2.0)
    if row.get("last_confirmed_at"):
        decayed = max(decayed, importance)  # the family reconfirmed it; no decay
    return overlap * 10.0 + decayed


def profile(patient_id: int, user: dict) -> list[dict]:
    return db.get_doctor_memories(patient_id, user, doctor_type=db.SHARED_AGENT, limit=MAX_PROFILE_FACTS)


def lessons(user: dict, limit: int = MAX_LESSONS) -> list[dict]:
    return db.get_lessons(user, limit=limit)


def agent_notes(patient_id: int, user: dict, agent_type: str, query: str = "", limit: int = MAX_NOTES_PER_AGENT) -> list[dict]:
    rows = db.get_doctor_memories(patient_id, user, doctor_type=agent_type, limit=200)
    qw = _words(query)
    now = _dt.datetime.now()
    return sorted(rows, key=lambda r: (-score(r, qw, now), -(r.get("id") or 0)))[:limit]


def context_block(patient_id: int, user: dict) -> str:
    """The shared part of every specialist's context: profile and lessons."""
    lines: list[str] = []
    facts = profile(patient_id, user)
    if facts:
        lines.append("\nPATIENT PROFILE (facts the family and the records have established; the family can edit these):")
        for f in facts:
            tag = f" [{f['category']}]" if f.get("category") else ""
            lines.append(f"- {f['content'][:300]}{tag}")
    less = lessons(user)
    if less:
        lines.append("\nLESSONS FROM THE FAMILY'S CORRECTIONS (apply to every patient):")
        for l in less:
            lines.append(f"- {l['content'][:300]}")
    return "\n".join(lines)


def notes_block(patient_id: int, user: dict, agent_type: str, query: str = "") -> str:
    notes = agent_notes(patient_id, user, agent_type, query)
    if not notes:
        return ""
    lines = [f"\nYOUR OWN EARLIER NOTES ({agent_type}), most relevant first:"]
    for n in notes:
        when = n["created_at"].strftime("%Y-%m-%d") if isinstance(n.get("created_at"), _dt.datetime) else ""
        lines.append(f"- ({when}) {n['content'][:300]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Consolidation after a board run
# ---------------------------------------------------------------------------

class ProfileFact(BaseModel):
    text: str
    category: Literal["diagnosis", "allergy", "baseline", "doctor", "preference", "instruction", "other"]
    importance: int
    replaces_ids: list[int]          # ids of existing profile facts this one supersedes


class ProfileUpdate(BaseModel):
    new_facts: list[ProfileFact]
    contradictions: list[str]        # where a new finding disagrees with the profile or a lesson
    unchanged: bool                  # true when nothing new was established


CONSOLIDATE_SYSTEM = (
    "You maintain the shared profile of one patient for a family's record system. You are given the current profile "
    "facts (each with an id) and what the specialists concluded in the latest review of the records. "
    "Return only facts that are established by the records or stated by the family: diagnoses, allergies, baseline "
    "values and their dates, who the treating doctors are, preferences, standing instructions doctors gave. "
    "Keep each fact to one sentence. When a new fact replaces an older one (a newer value, a changed medication list, a "
    "corrected name), put the old fact's id in replaces_ids. Never repeat a fact that is already in the profile; set "
    "unchanged to true if nothing new was established. List contradictions between the new findings and the profile or "
    "the lessons as short sentences. Never add advice, predictions or recommendations."
)


def consolidate(provider: Provider, patient_id: int, user: dict, specialist_outputs: list[dict],
                run_id: int | None = None, model: str | None = None) -> dict[str, Any]:
    """Merge a run's findings into the profile. Returns what changed."""
    model = model or DEFAULT_MODELS.get("memory") or DEFAULT_MODELS["router"]
    facts = profile(patient_id, user)
    lines = ["CURRENT PROFILE:"] + [f"- id {f['id']}: {f['content']}" for f in facts] if facts else ["CURRENT PROFILE: empty"]
    less = lessons(user)
    if less:
        lines.append("LESSONS FROM THE FAMILY:")
        lines += [f"- {l['content'][:300]}" for l in less]
    lines.append("LATEST REVIEW:")
    for o in specialist_outputs:
        lines.append(f"[{o.get('agent_type')}] {o.get('summary', '')}")
        for ob in o.get("observations", [])[:6]:
            lines.append(f"  - {ob.get('text')}")
    out: dict[str, Any] = {"added": [], "superseded": 0, "contradictions": [], "call": None, "error": None}
    try:
        update, call = provider.structured(model=model, system=CONSOLIDATE_SYSTEM, parts=[Part.of_text("\n".join(lines))],
                                           schema=ProfileUpdate, purpose="memory_consolidate", max_tokens=1500)
    except ProviderError as e:
        out["error"] = str(e)
        return out
    out["call"] = call.as_dict()
    existing = {f["id"] for f in facts}
    known = {f["content"].strip().lower() for f in facts}
    for fact in update.new_facts:
        text = fact.text.strip()
        if not text or text.lower() in known:
            continue
        new_id = db.save_doctor_memory(patient_id, user, db.SHARED_AGENT, "fact", text,
                                       importance=max(1, min(10, fact.importance)), source_run_id=run_id, category=fact.category)
        out["added"].append({"id": new_id, "text": text, "category": fact.category})
        old = [i for i in fact.replaces_ids if i in existing]
        if old:
            out["superseded"] += db.supersede_doctor_memories(patient_id, user, old, new_id)
        known.add(text.lower())
    for c in update.contradictions[:5]:
        if c.strip():
            db.save_doctor_memory(patient_id, user, "coordinator", "contradiction", c.strip(), importance=7, source_run_id=run_id,
                                  category="other")
            out["contradictions"].append(c.strip())
    return out


# ---------------------------------------------------------------------------
# What a conversation teaches
# ---------------------------------------------------------------------------

def record_consultation(patient_id: int, user: dict, summary: dict, consultation_id: int, file_id: int | None,
                        happened_on: _dt.date | None) -> list[int]:
    """Standing instructions and the doctor's identity go into the profile;
    the visit itself becomes a general duty note."""
    ids: list[int] = []
    when = happened_on.isoformat() if happened_on else "an unknown date"
    doctor = summary.get("doctor_name")
    if doctor:
        text = f"{doctor}" + (f" ({summary['doctor_specialty']})" if summary.get("doctor_specialty") else "") + f" saw the patient on {when}."
        ids.append(db.save_doctor_memory(patient_id, user, db.SHARED_AGENT, "fact", text, importance=5,
                                         source_consultation_id=consultation_id, source_file_id=file_id, category="doctor"))
    for a in summary.get("actions", []):
        if a.get("kind") in ("monitoring", "test", "medication", "lifestyle", "referral") and (a.get("confidence") or 0) >= 0.6:
            who = f"{doctor} asked" if doctor else "The doctor asked"
            text = f"{who} on {when}: {a.get('text')}" + (f" ({a['when']})" if a.get("when") else "")
            ids.append(db.save_doctor_memory(patient_id, user, db.SHARED_AGENT, "standing_instruction", text, importance=6,
                                             source_consultation_id=consultation_id, source_file_id=file_id, category="instruction"))
    memo = f"Doctor conversation on {when}" + (f" with {doctor}" if doctor else "") + f": {summary.get('summary', '')}"
    ids.append(db.save_doctor_memory(patient_id, user, "general_duty", "consultation", memo[:900], importance=5,
                                     source_file_id=file_id, source_consultation_id=consultation_id))
    return ids
