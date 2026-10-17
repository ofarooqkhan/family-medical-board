"""Board orchestration, modeled on how a multidisciplinary team meeting runs.

One run of the board is one case discussion in an ongoing conversation:

  1. Triage (the chair). A small model reads the question, the conversation
     so far, the record index and the open items from the last review, and
     decides which specialists are needed and the single question each must
     answer. A question about the record itself (what is on file, what the
     medication list says) is answered by the chair without convening anyone.
     When the chair call fails, keyword routing takes over.
  2. Workup by role. Each specialist gets the slice of the record its domain
     covers (its documents, every latest value, the medication list, the
     shared profile, its own earlier notes) and returns findings only, each
     tied to a document and page. No questions at this stage.
  3. Discussion. Each specialist reads the others' findings and replies:
     agree, disagree or add. Only then does it write questions for the
     treating doctor, each resting on a named finding.
  4. Synthesis (the chair again). One brief: what changed, what the records
     show, where the specialists differ, the questions that survived, and the
     plan state: every open item from the last review marked answered,
     superseded or still open, plus the new ones. The next review starts
     from that list instead of generating questions afresh.

Nothing here advises: prompts forbid it, advice-seeking queries are detected
before the run and answered with a redirect, and outputs are scrubbed for
advice phrasing afterwards. Every model call is logged with its cost.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel

import database as db
from providers import DEFAULT_MODELS, ModelCall, Part, Provider, ProviderError

MAX_SPECIALISTS = 5
HISTORY_TURNS = 8
REDIRECT = ("This board does not give medical advice. It can tell you what the records show and what to ask. "
            "Please put this question to the treating doctor.")

# ---------------------------------------------------------------------------
# Scope enforcement (item 11)
# ---------------------------------------------------------------------------

_ADVICE_PATTERNS = [
    r"\bshould (i|we|he|she|they|my (mother|father|mom|dad|parent))\b.*\b(take|give|stop|start|increase|decrease|reduce|double|skip|switch|continue)\b",
    r"\b(can|could|may) (i|we|he|she|they)\b.*\b(take|give|stop|start|increase|decrease|skip|double|switch|combine|mix)\b",
    r"\bhow (much|many)\b.*\b(should|can|to give|to take|dose|tablets?|mg|units?)\b",
    r"\bwhat (dose|dosage|strength)\b.*\b(should|give|take|start|use)\b",
    r"\b(increase|decrease|reduce|lower|raise|double|halve|adjust|titrate|change) (the |his |her |their )?(dose|dosage|insulin|medication|medicine|tablets?)\b",
    r"\b(stop|start|restart|discontinue|hold|skip|pause) (giving |taking |the |his |her |their )?[a-z][a-z\- ]{2,40}\b.*\?",
    r"\bis it (safe|ok|okay|fine|alright) (to|for)\b",
    r"\bwhich (medicine|medication|drug|tablet|antibiotic|painkiller|insulin)\b.*\b(better|best|should|give|take|use)\b",
    r"\b(recommend|suggest|prescribe|advise)\b.*\b(medicine|medication|drug|dose|treatment|therapy|tablet|antibiotic|supplement)\b",
    r"\bwhat (medicine|medication|drug|treatment|antibiotic|painkiller) (should|can|to) (i|we|he|she|they)?\s?(give|take|use|try)\b",
    r"\b(treat|cure|fix|manage) (this|his|her|their|the)\b.*\b(at home|myself|ourselves|without (a |the )?doctor)\b",
    r"\bdiagnos(e|is)\b.*\b(what (is|does) (he|she|it|this) (have|mean))\b",
    r"\bdoes (he|she|my (mother|father|mom|dad)) (have|need)\b.*\b(cancer|stroke|dementia|infection|surgery|dialysis|antibiotics?|insulin)\b",
    r"\bneed(s)? (to )?(see|go to) (a |the )?(doctor|hospital|er|emergency)\b",
    r"\b(home remed|natural (cure|treatment)|instead of (the )?(medicine|medication|doctor))\b",
]
_ADVICE_RE = [re.compile(p, re.IGNORECASE) for p in _ADVICE_PATTERNS]

_OUTPUT_ADVICE_RE = re.compile(
    r"\b(you should (take|give|stop|start|increase|decrease|switch)|i recommend|we recommend|should be (increased|decreased|stopped|started|adjusted|switched)|"
    r"(increase|decrease|reduce|stop|start|discontinue) (the |his |her )?(dose|dosage|medication|medicine)\b|"
    r"(consider|try) (taking|giving|starting|stopping|switching)|take \d+ ?(mg|tablets?|units?)|"
    r"(would|may) benefit from (starting|stopping|a higher|a lower|switching))",
    re.IGNORECASE,
)


def detect_advice_request(query: str) -> list[str]:
    """Return the patterns an advice-seeking query matched; empty for record questions."""
    text = " ".join((query or "").split())
    return [p.pattern for p in _ADVICE_RE if p.search(text)]


def scrub_advice(text: str) -> tuple[str, int]:
    """Remove sentences that read as advice. Returns (text, removed_count)."""
    if not text:
        return text, 0
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if not _OUTPUT_ADVICE_RE.search(s)]
    removed = len(sentences) - len(kept)
    return (" ".join(kept).strip() or REDIRECT), removed


# ---------------------------------------------------------------------------
# Schemas. Optional fields default to None, never to a value: the OpenAI
# strict schema accepts a null default and rejects any other.
# ---------------------------------------------------------------------------

class SourceRef(BaseModel):
    file_id: Optional[int] = None
    page: Optional[int] = None
    label: str  # e.g. "Lab report 2026-09-05, page 1" or "Medication list"


class Observation(BaseModel):
    text: str
    category: Literal["value", "trend", "change", "finding", "gap", "other"]
    sources: list[SourceRef]
    confidence: float


class DoctorQuestion(BaseModel):
    question: str
    for_doctor: str  # specialty or named prescriber
    why: str
    priority: Literal["high", "medium", "low"]
    based_on: Optional[str] = None  # the finding this question rests on


class CrossDomainFlag(BaseModel):
    item: str
    also_concerns: list[str]  # agent_types
    why: str


class SpecialistFindings(BaseModel):
    """Stage 2: what one specialist found in its slice of the record."""
    agent_type: str
    summary: str
    observations: list[Observation]
    missing_records: list[str]
    cross_domain_flags: list[CrossDomainFlag]
    declined_advice_request: bool
    decline_note: Optional[str] = None
    confidence: float


class PeerResponse(BaseModel):
    to: str  # agent_type of the specialist being answered
    stance: Literal["agree", "disagree", "add"]
    text: str


class DiscussionTurn(BaseModel):
    """Stage 3: one specialist's reply to the others, then its questions."""
    agent_type: str
    responses: list[PeerResponse]
    added_observations: list[Observation]
    questions_for_doctor: list[DoctorQuestion]
    confidence: float


class SpecialistOutput(BaseModel):
    """Findings plus discussion, stored per specialist per run."""
    agent_type: str
    summary: str
    observations: list[Observation]
    questions_for_doctor: list[DoctorQuestion]
    missing_records: list[str]
    cross_domain_flags: list[CrossDomainFlag]
    declined_advice_request: bool
    decline_note: Optional[str] = None
    confidence: float
    assigned_question: Optional[str] = None
    discussion: Optional[list[PeerResponse]] = None


class QuestionsForDoctor(BaseModel):
    doctor: str
    questions: list[str]


class OpenItem(BaseModel):
    text: str
    doctor: str
    status: Literal["open", "answered", "superseded"]
    since_run_id: Optional[int] = None


class CoordinatorOutput(BaseModel):
    headline: str
    what_changed: list[str]
    key_observations: list[Observation]
    questions_by_doctor: list[QuestionsForDoctor]
    missing_records: list[str]
    disagreements: list[str]
    declined_advice_request: bool
    decline_note: Optional[str] = None
    open_items: Optional[list[OpenItem]] = None


class RoutingDecision(BaseModel):
    agents: list[str]
    reason: str


class TriageAssignment(BaseModel):
    agent_type: str
    question: str  # the one question this specialist must answer


class TriageDecision(BaseModel):
    assignments: list[TriageAssignment]
    direct_answer: Optional[str] = None  # set, with no assignments, when the index answers the question
    reason: str


# ---------------------------------------------------------------------------
# The record, loaded once and sliced per specialist
# ---------------------------------------------------------------------------

@dataclass
class Record:
    header: list[str] = field(default_factory=list)
    meds: list[str] = field(default_factory=list)
    vitals: list[str] = field(default_factory=list)
    doc_index: list[str] = field(default_factory=list)
    docs: list[dict] = field(default_factory=list)  # id, kind, date, filename, text; most recent first
    profile: str = ""
    appointments: list[str] = field(default_factory=list)


def load_record(patient_id: int, user: dict, max_doc_chars: int = 3000, max_docs: int = 15) -> Record:
    patient = db.get_patient(patient_id, user) or {}
    rec = Record()
    rec.header.append(f"PATIENT: {patient.get('name')}")
    for k in ("age", "sex", "date_of_birth", "weight_lbs", "allergies", "medical_history", "primary_doctor", "notes"):
        if patient.get(k):
            rec.header.append(f"{k.replace('_', ' ').title()}: {patient[k]}")

    try:
        import medications
        meds = medications.list_medications(patient_id, user, status="active")
    except Exception:
        meds = []
    rec.meds.append("ACTIVE MEDICATIONS (confirmed by the family from documents):")
    if meds:
        for m in meds:
            src = f" [doc {m['source_file_id']} p{m['source_page']}]" if m.get("source_file_id") else ""
            rec.meds.append(f"- {m['name']} {m.get('dose') or ''} {m.get('frequency') or ''}; prescriber: {m.get('prescriber') or 'unknown'}; "
                            f"since {m.get('start_date') or 'unknown'}{src}")
    else:
        rec.meds.append("- none recorded")
    if patient.get("medications"):
        rec.meds.append(f"Free-text medication note: {patient['medications']}")

    rec.vitals.append("LATEST VALUES (with previous value where known):")
    latest = db.get_latest_vitals(patient_id, user)
    if latest:
        for v in latest:
            when = v["recorded_at"] or v["timestamp"]
            change = f", change {v['change_from_previous']:+.1f}" if v.get("change_from_previous") is not None else ""
            ref = f", ref {v['reference_range']}" if v.get("reference_range") else ""
            flag = f", {v['flag']}" if v.get("flag") and v["flag"] not in ("normal", "unknown") else ""
            src = f" [doc {v['source_file_id']} p{v['source_page']}]" if v.get("source_file_id") else ""
            rec.vitals.append(f"- {v['reading_type']}: {v['value']} {v['unit'] or ''} on {str(when)[:10]}{change}{ref}{flag}{src}")
    else:
        rec.vitals.append("- none recorded")

    rec.doc_index.append("DOCUMENTS ON FILE (id, kind, date, status):")
    docs = db.list_documents(patient_id, user)
    for d in docs[:40]:
        rec.doc_index.append(f"- doc {d['id']}: {d.get('document_kind') or d.get('file_type') or 'unknown'}, {d.get('document_date') or 'no date'}, "
                             f"{d['filename']}, {d['status']}")
    for d in docs[:max_docs]:
        full = db.get_document(d["id"], patient_id, user)
        body = full.get("extraction") if full else None
        if body and isinstance(body, dict):
            chunk = json.dumps(body.get("document", body), ensure_ascii=False)[:max_doc_chars]
        elif full and full.get("extracted_text"):
            chunk = full["extracted_text"][:max_doc_chars // 2]
        else:
            continue
        rec.docs.append({"id": d["id"], "kind": d.get("document_kind") or "", "date": str(d.get("document_date") or ""),
                         "filename": d["filename"], "text": chunk})

    try:
        import memory
        rec.profile = memory.context_block(patient_id, user) or ""
    except Exception:
        rec.profile = ""

    try:
        import appointments
        upcoming = appointments.list_appointments(patient_id, user, upcoming_only=True, limit=5)
        if upcoming:
            rec.appointments.append("UPCOMING APPOINTMENTS:")
            for a in upcoming:
                rec.appointments.append(f"- {a['starts_at']}: {a['title']} with {a.get('doctor_name') or a.get('specialty') or 'unknown'}")
    except Exception:
        pass
    return rec


def record_index(rec: Record) -> str:
    """Everything but the document bodies: what triage and the chair read."""
    parts = ["\n".join(rec.header), "\n".join(rec.meds), "\n".join(rec.vitals), "\n".join(rec.doc_index)]
    if rec.profile:
        parts.append(rec.profile.strip())
    if rec.appointments:
        parts.append("\n".join(rec.appointments))
    return "\n\n".join(p for p in parts if p)


_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_NUM_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.]*\d)")


def _numbers(text: str) -> set[float]:
    """Numbers a statement asserts: values and ranges. Dates, years and small
    whole numbers (counts, months, tablets) are not checked."""
    out: set[float] = set()
    for tok in _NUM_RE.findall(_DATE_RE.sub(" ", text or "")):
        try:
            v = float(tok)
        except ValueError:
            continue
        if "." not in tok and (v <= 12 or 1900 <= v <= 2100):
            continue
        out.add(round(v, 3))
    return out


def record_text(rec: "Record") -> str:
    return record_index(rec) + "\n" + "\n".join(d["text"] for d in rec.docs)


def ungrounded(text: str, known: set[float]) -> list[float]:
    """Numbers in a statement that appear nowhere in the record."""
    return sorted(n for n in _numbers(text) if n not in known)


def ground_findings(f: "SpecialistFindings", known: set[float]) -> list[str]:
    """Drop observations that state a number the record does not contain, so a
    misread value never reaches the family or the memory. Returns what was dropped."""
    dropped = []
    keep = []
    for o in f.observations:
        bad = ungrounded(o.text, known)
        if bad:
            dropped.append(f"{f.agent_type}: {o.text} (not in record: {', '.join(str(b) for b in bad)})")
        else:
            keep.append(o)
    f.observations = keep
    if ungrounded(f.summary, known):
        dropped.append(f"{f.agent_type} summary: {f.summary}")
        f.summary = " ".join(o.text for o in keep[:2]) or "See the observations below."
    return dropped


def ground_coordinator(co: "CoordinatorOutput", known: set[float]) -> list[str]:
    dropped = []
    kept_obs = []
    for o in co.key_observations:
        if ungrounded(o.text, known):
            dropped.append(f"coordinator: {o.text}")
        else:
            kept_obs.append(o)
    co.key_observations = kept_obs
    kept_changes = []
    for c in co.what_changed:
        if ungrounded(c, known):
            dropped.append(f"coordinator what changed: {c}")
        else:
            kept_changes.append(c)
    co.what_changed = kept_changes
    if ungrounded(co.headline, known):
        dropped.append(f"coordinator headline: {co.headline}")
        co.headline = kept_obs[0].text if kept_obs else "Here is what the records show."
    return dropped


def _doc_entry(d: dict) -> str:
    return f"--- doc {d['id']} ({d['kind']}, {d['date'] or 'no date'}) ---\n{d['text']}"


# Documents a role always reads regardless of keyword hits, and roles that read the whole record.
_KIND_OWNERS = {"radiologist": ("imaging_report",), "pharmacist": ("prescription", "medication_list")}
_READS_EVERYTHING = {"general_duty", "geriatrician", "coordinator"}
_SHARED_KINDS = ("discharge_summary",)


def _doc_matches(agent: dict, d: dict) -> bool:
    t = agent.get("agent_type", "")
    if t in _READS_EVERYTHING or d["kind"] in _SHARED_KINDS or d["kind"] in _KIND_OWNERS.get(t, ()):
        return True
    text = f"{d['kind']} {d['filename']} {d['text']}".lower()
    return _domain_hits(agent.get("domains") or [], text) >= 1


def slice_record(rec: Record, agent: dict, max_chars: int = 14000) -> str:
    """The part of the record one specialist works from. Document bodies are
    chosen by the specialist's domain; the index, values, medication list and
    profile are shared by everyone, as the chart is in a real meeting."""
    lines = [record_index(rec), ""]
    chosen = [d for d in rec.docs if _doc_matches(agent, d)]
    note = "EXTRACTED CONTENT OF THE DOCUMENTS IN YOUR DOMAIN (most recent first):"
    if not chosen and rec.docs:
        chosen = rec.docs[:3]
        note = "No document on file matches your domain. The most recent documents follow so you can say what is missing:"
    lines.append(note)
    budget = max_chars - sum(len(l) + 1 for l in lines)
    for d in chosen:
        entry = _doc_entry(d)
        if len(entry) > budget:
            break
        lines.append(entry)
        budget -= len(entry) + 1
    if not chosen:
        lines.append("- no documents on file")
    return "\n".join(lines)


def build_context(patient_id: int, user: dict, max_chars: int = 24000) -> str:
    """The whole record as one text (kept for callers that want everything)."""
    rec = load_record(patient_id, user)
    return slice_record(rec, {"agent_type": "general_duty", "domains": []}, max_chars=max_chars)


# ---------------------------------------------------------------------------
# Conversation and plan state
# ---------------------------------------------------------------------------

def conversation_turns(conversation_id: int | None, user: dict, query: str, limit: int = HISTORY_TURNS) -> list[dict]:
    """Earlier turns of this conversation, oldest first, trimmed. The current
    question is excluded when the caller has already stored it."""
    if not conversation_id:
        return []
    try:
        rows = db.get_conversation_history(conversation_id, user)
    except Exception:
        return []
    if rows and rows[-1]["role"] == "user" and (rows[-1]["content"] or "").strip() == (query or "").strip():
        rows = rows[:-1]
    turns = []
    for r in rows[-limit:]:
        content = r.get("content") or ""
        if r["role"] != "user":
            content = "\n".join(l for l in content.splitlines() if not l.startswith("_Specialists:") and not l.startswith("> "))
        turns.append({"role": "family" if r["role"] == "user" else "board", "content": content.strip()[:800]})
    return turns


def _history_text(turns: list[dict]) -> str:
    if not turns:
        return "CONVERSATION SO FAR: this is the first question."
    return "CONVERSATION SO FAR (oldest first):\n" + "\n".join(f"[{t['role']}] {t['content']}" for t in turns)


def previous_open_items(patient_id: int, user: dict) -> list[dict]:
    """The plan state the last review left: its open items, or, for a run made
    before plan state existed, its questions."""
    for run in list_runs(patient_id, user, limit=5):
        co = run.get("coordinator_output") or {}
        if isinstance(co, str):
            co = json.loads(co)
        if not co:
            continue
        if co.get("open_items") is not None:
            return [i for i in co["open_items"] if i.get("status") == "open"]
        items = []
        for g in co.get("questions_by_doctor", []):
            for q in g.get("questions", []):
                items.append({"text": q, "doctor": g.get("doctor") or "treating doctor", "status": "open", "since_run_id": run["id"]})
        return items
    return []


def _items_text(items: list[dict]) -> str:
    if not items:
        return "OPEN ITEMS FROM THE LAST REVIEW: none."
    return "OPEN ITEMS FROM THE LAST REVIEW (still waiting on a doctor):\n" + "\n".join(
        f"- [{i.get('doctor')}] {i.get('text')}" + (f" (since run {i['since_run_id']})" if i.get("since_run_id") else "") for i in items)


# ---------------------------------------------------------------------------
# Stage 1: triage by the chair (keyword routing as the fallback)
# ---------------------------------------------------------------------------

def _domain_hits(domains: list[str], text: str) -> int:
    """Whole-word matches only: "ast" must not match "last", "bp" must not match "bpm"."""
    return sum(1 for d in domains if re.search(r"(?<![a-z0-9])" + re.escape(d.lower()) + r"(?![a-z0-9])", text))


def keyword_route(query: str, context: str, agents: list[dict]) -> list[str]:
    text = f"{query}\n{context}".lower()
    q = query.lower()
    hits: list[tuple[int, str]] = []
    for a in agents:
        if a["agent_type"] == "coordinator":
            continue
        domains = a.get("domains") or []
        q_hits = _domain_hits(domains, q)
        c_hits = _domain_hits(domains, text)
        score = q_hits * 10 + min(c_hits, 5)
        if q_hits or (c_hits >= 2 and not q.strip()):
            hits.append((score, a["agent_type"]))
    hits.sort(key=lambda h: -h[0])  # stable: ties keep the roster's sort order
    return [t for _, t in hits]


def route(provider: Provider, query: str, context: str, agents: list[dict], model: str | None = None,
          force_all: bool = False) -> tuple[list[str], str, ModelCall | None]:
    """Fallback routing when the chair is unavailable: keyword match first, model second."""
    specialists = [a for a in agents if a["agent_type"] != "coordinator"]
    names = [a["agent_type"] for a in specialists]
    if force_all or not query.strip() or re.search(r"\b(full|complete|whole|overall|everything|all specialists|general review)\b", query, re.I):
        return names, "full review requested", None
    chosen = keyword_route(query, context, specialists)
    call = None
    if len(chosen) < 1:
        model = model or DEFAULT_MODELS["router"]
        menu = "\n".join(f"- {a['agent_type']}: {a['display_name']}; domains: {', '.join(a.get('domains') or [])}" for a in specialists)
        system = ("You route a caregiver's question about an elderly patient's records to the specialists whose domain it "
                  "touches. Pick the fewest that can answer it, at most five. Use only agent_type values from the list.")
        parts = [Part.of_text(f"Specialists:\n{menu}\n\nQuestion: {query}\n\nRecord summary (first 3000 chars):\n{context[:3000]}")]
        try:
            decision, call = provider.structured(model=model, system=system, parts=parts, schema=RoutingDecision,
                                                 purpose="route", max_tokens=300)
            chosen = [a for a in decision.agents if a in names]
            reason = decision.reason
        except ProviderError as e:
            chosen, reason = [], f"router failed: {e}"
    else:
        reason = "keyword match"
    if not chosen:
        chosen = ["general_duty"] if "general_duty" in names else names[:1]
        reason += "; fell back to general duty"
    if "pharmacist" in names and "pharmacist" not in chosen and re.search(r"\b(medic|drug|tablet|dose|prescri|pharm)", query, re.I):
        chosen.append("pharmacist")
    return chosen[:MAX_SPECIALISTS], reason, call


TRIAGE_SYSTEM = (
    "You chair a family's record-review board for an elderly patient. The board never gives medical advice: it says what "
    "the records show and what to ask the treating doctors. Decide how this question is handled.\n"
    "- If it is about the record itself and the index below answers it verbatim (which documents are on file, when something "
    "was added, what the medication list says and who prescribed it, the latest value of a reading), put the answer in "
    "direct_answer in one or two sentences and leave assignments empty. A direct answer repeats the record; it never "
    "interprets it.\n"
    "- Otherwise convene the fewest specialists that can answer it, usually one and at most {max_n}, and give each one a "
    "single precise question to answer from its records. Read the conversation so far: a follow-up such as 'and the "
    "kidneys?' continues the earlier turns. When the family asks for a full or overall review, convene every specialist "
    "whose domain has records on file.\n"
    "- Each specialist's question asks what the records show. Never pass on a request for a recommendation, a dose, a "
    "change of treatment or what the family should do: reword it into what the records show on that topic and what the "
    "treating doctor should be asked.\n"
    "- Use only agent_type values from the list. Give the reason in one sentence."
)


def triage(provider: Provider, query: str, index: str, agents: list[dict], history: list[dict] | None = None,
           open_items: list[dict] | None = None, model: str | None = None, force_all: bool = False
           ) -> tuple[list[dict], str, ModelCall | None, str | None]:
    """Returns (assignments, reason, call, direct_answer). Each assignment is
    {agent_type, question}."""
    specialists = [a for a in agents if a["agent_type"] != "coordinator"]
    names = [a["agent_type"] for a in specialists]
    if force_all or not query.strip():
        q = query or "Please review everything in your domain."
        return [{"agent_type": n, "question": q} for n in names[:MAX_SPECIALISTS]], "full review requested", None, None
    model = model or DEFAULT_MODELS["router"]
    menu = "\n".join(f"- {a['agent_type']}: {a['display_name']}; domains: {', '.join(a.get('domains') or [])}" for a in specialists)
    user_text = (f"SPECIALISTS AVAILABLE:\n{menu}\n\n{_history_text(history or [])}\n\n{_items_text(open_items or [])}\n\n"
                 f"RECORD INDEX:\n{index[:6000]}\n\nFAMILY'S QUESTION: {query}")
    try:
        decision, call = provider.structured(model=model, system=TRIAGE_SYSTEM.format(max_n=MAX_SPECIALISTS),
                                             parts=[Part.of_text(user_text)], schema=TriageDecision, purpose="triage", max_tokens=600)
    except ProviderError as e:
        chosen, reason, call = route(provider, query, index, agents, force_all=force_all)
        return [{"agent_type": t, "question": query} for t in chosen], f"chair failed ({e}); {reason}", call, None
    assignments, seen = [], set()
    for a in decision.assignments:
        if a.agent_type in names and a.agent_type not in seen:
            assignments.append({"agent_type": a.agent_type, "question": a.question.strip() or query})
            seen.add(a.agent_type)
    if not assignments and decision.direct_answer and decision.direct_answer.strip():
        return [], decision.reason, call, decision.direct_answer.strip()
    reason = decision.reason
    if not assignments:
        assignments = [{"agent_type": t, "question": query} for t in (["general_duty"] if "general_duty" in names else names[:1])]
        reason += "; fell back to general duty"
    return assignments[:MAX_SPECIALISTS], reason, call, None


# ---------------------------------------------------------------------------
# Stages 2 and 3: findings, then discussion
# ---------------------------------------------------------------------------

def _lang(language: str) -> str:
    return "Urdu" if language == "ur" else "English"


def run_findings(provider: Provider, agent: dict, assignment: str, query: str, context: str, language: str,
                 advice_flagged: bool, model: str | None = None, notes: str = "") -> tuple[SpecialistFindings, ModelCall]:
    model = model or agent.get("model") or DEFAULT_MODELS["specialist"]
    if notes:
        context = context + "\n" + notes
    system = agent.get("system_prompt") or agent.get("backstory") or ""
    system += (f"\nSet agent_type to '{agent['agent_type']}'. Write in {_lang(language)}. The chair has asked you one question; "
               "answer it from the records given, with every observation tied to a document id and page from the record. "
               "Report findings only: values, trends, changes, gaps. Questions for the doctor come later, in the discussion. "
               "Flag anything another specialist should see in cross_domain_flags. In missing_records name the record that "
               "would let you answer better, if any.")
    if advice_flagged:
        system += ("\nThe family's question asks for advice. Set declined_advice_request to true, put one sentence in decline_note "
                   "that points to the treating doctor, and still report what the records show on the topic.")
    user_text = f"RECORDS:\n{context}\n\nFAMILY'S QUESTION: {query or 'full review'}\n\nTHE CHAIR'S QUESTION TO YOU: {assignment}"
    out, call = provider.structured(model=model, system=system, parts=[Part.of_text(user_text)],
                                    schema=SpecialistFindings, purpose=f"specialist:{agent['agent_type']}", max_tokens=3000)
    out.agent_type = agent["agent_type"]
    out.summary, _ = scrub_advice(out.summary)
    for o in out.observations:
        o.text, _ = scrub_advice(o.text)
    return out, call


def _findings_for_peers(f: SpecialistFindings) -> dict:
    return {"agent_type": f.agent_type, "summary": f.summary,
            "observations": [o.text for o in f.observations],
            "cross_domain_flags": [x.model_dump() for x in f.cross_domain_flags],
            "missing_records": f.missing_records, "confidence": f.confidence}


def run_discussion(provider: Provider, agent: dict, query: str, own: SpecialistFindings, peers: list[SpecialistFindings],
                   language: str, advice_flagged: bool, history: list[dict] | None = None,
                   open_items: list[dict] | None = None, model: str | None = None) -> tuple[DiscussionTurn, ModelCall]:
    model = model or agent.get("model") or DEFAULT_MODELS["specialist"]
    system = agent.get("system_prompt") or agent.get("backstory") or ""
    system += (f"\nSet agent_type to '{agent['agent_type']}'. Write in {_lang(language)}. The board is in discussion. You have "
               "your own findings and the other specialists' findings. For each point of another specialist that touches your "
               "domain, reply: agree, disagree (say why, from the records) or add (what they missed; put it in "
               "added_observations with its source). Then write your questions for the treating doctor: only a question that "
               "rests on a named finding (put it in based_on) and bears on the family's question. Do not repeat an item that is "
               "already open from the last review. An empty list is the right answer when the records are clear. Never advise.")
    if advice_flagged:
        system += "\nThe family's question asks for advice; do not answer it. Questions for the doctor may cover the topic."
    peer_text = json.dumps([_findings_for_peers(p) for p in peers], ensure_ascii=False) if peers else "No other specialist took part."
    user_text = (f"FAMILY'S QUESTION: {query or 'full review'}\n\n{_history_text(history or [])}\n\n{_items_text(open_items or [])}\n\n"
                 f"YOUR FINDINGS (JSON):\n{json.dumps(own.model_dump(), ensure_ascii=False)}\n\n"
                 f"OTHER SPECIALISTS' FINDINGS (JSON):\n{peer_text}")
    out, call = provider.structured(model=model, system=system, parts=[Part.of_text(user_text)],
                                    schema=DiscussionTurn, purpose=f"discussion:{agent['agent_type']}", max_tokens=2000)
    out.agent_type = agent["agent_type"]
    for o in out.added_observations:
        o.text, _ = scrub_advice(o.text)
    for r in out.responses:
        r.text, _ = scrub_advice(r.text)
    return out, call


def merge_specialist(findings: SpecialistFindings, turn: DiscussionTurn | None, assignment: str) -> SpecialistOutput:
    return SpecialistOutput(
        agent_type=findings.agent_type, summary=findings.summary,
        observations=findings.observations + (turn.added_observations if turn else []),
        questions_for_doctor=turn.questions_for_doctor if turn else [],
        missing_records=findings.missing_records, cross_domain_flags=findings.cross_domain_flags,
        declined_advice_request=findings.declined_advice_request, decline_note=findings.decline_note,
        confidence=turn.confidence if turn else findings.confidence, assigned_question=assignment,
        discussion=turn.responses if turn else None)


# ---------------------------------------------------------------------------
# Stage 4: synthesis by the chair
# ---------------------------------------------------------------------------

def run_coordinator(provider: Provider, agent: dict | None, query: str, outputs: list[SpecialistOutput], language: str,
                    advice_flagged: bool, history: list[dict] | None = None, open_items: list[dict] | None = None,
                    model: str | None = None, patient_first_name: str | None = None) -> tuple[CoordinatorOutput, ModelCall]:
    model = model or (agent or {}).get("model") or DEFAULT_MODELS["coordinator"]
    system = (agent or {}).get("system_prompt") or (
        "You are the care coordinator. Merge the specialists' structured outputs into one brief for the family. "
        "Never give medical advice.")
    system += (f"\nWrite in {_lang(language)}. You chair the synthesis of a board discussion, replying in an ongoing conversation "
               "with the family. Answer their question first, in the headline, in plain words. Then: what changed; what the "
               "records show, keeping every source reference; where the specialists disagreed and why, from their discussion "
               "stances. Keep a question only when it rests on a named finding and bears on the family's question; merge "
               "duplicates and group them by the doctor who should hear them. Plan state: in open_items list every item from "
               "the last review with its new status (answered when this review's findings answer it, superseded when a kept "
               "question replaces it, otherwise open, keeping its since_run_id), then add each kept question as a new open item "
               "with status open and since_run_id null. what_changed lists only differences: a value against an earlier "
               "value of the same test, a medication started, stopped or changed, or something new since the earlier turns of "
               "this conversation. A single reading with nothing to compare is not a change; leave what_changed empty rather "
               "than restate values. Quote every number exactly as it appears in the specialists' findings.")
    if patient_first_name:
        system += f"\nRefer to the patient as {patient_first_name}, never as 'the patient'."
    if advice_flagged:
        system += "\nThe family asked for advice. Set declined_advice_request true and decline_note to a one-sentence redirect."
    else:
        system += "\nThe family's question is a question about the records. Set declined_advice_request false and decline_note null."
    payload = json.dumps([o.model_dump() for o in outputs], ensure_ascii=False)
    user_text = (f"FAMILY'S QUESTION: {query or 'full review'}\n\n{_history_text(history or [])}\n\n{_items_text(open_items or [])}\n\n"
                 f"SPECIALISTS' FINDINGS AND DISCUSSION (JSON):\n{payload}")
    out, call = provider.structured(model=model, system=system, parts=[Part.of_text(user_text)],
                                    schema=CoordinatorOutput, purpose="coordinator", max_tokens=3000)
    out.headline, _ = scrub_advice(out.headline)
    out.what_changed = [scrub_advice(s)[0] for s in out.what_changed]
    for o in out.key_observations:
        o.text, _ = scrub_advice(o.text)
    return out, call


def _carry_items(co: CoordinatorOutput, previous: list[dict], run_id: int) -> None:
    """Make the plan state complete: previous items the chair did not mention
    stay open; new items get this run's id."""
    items = list(co.open_items or [])
    origin = {(p.get("text") or "").strip().lower(): p.get("since_run_id") for p in previous}
    for i in items:  # an item the chair carried over keeps the run it was first raised in
        if origin.get(i.text.strip().lower()):
            i.since_run_id = origin[i.text.strip().lower()]
    seen = {i.text.strip().lower() for i in items}
    for p in previous:
        key = (p.get("text") or "").strip().lower()
        if key and key not in seen:
            items.append(OpenItem(text=p["text"], doctor=p.get("doctor") or "treating doctor", status="open",
                                  since_run_id=p.get("since_run_id")))
            seen.add(key)
    for g in co.questions_by_doctor:  # a kept question the chair left out of the plan is still open
        for q in g.questions:
            if q.strip().lower() not in seen:
                items.append(OpenItem(text=q, doctor=g.doctor, status="open"))
                seen.add(q.strip().lower())
    for i in items:
        if i.since_run_id is None:
            i.since_run_id = run_id
    co.open_items = items


def pharmacist_flags(patient_id: int, user: dict) -> list[dict[str, Any]]:
    """Deterministic reconciliation flags (item 9) added to the pharmacist's input."""
    try:
        import medications
        found = medications.find_duplicates(patient_id, user)
    except Exception:
        return []
    flags = []
    for group in found.get("duplicates", []):
        flags.append({"kind": "duplicate", "names": [m["name"] for m in group],
                      "prescribers": sorted({m.get("prescriber") or "unknown" for m in group})})
    for mp in found.get("multi_prescriber", []):
        flags.append({"kind": "multi_prescriber", "name": mp["name"], "prescribers": mp["prescribers"]})
    return flags


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_board(patient_id: int, user: dict, query: str, provider: Provider, language: str | None = None,
              conversation_id: int | None = None, force_all: bool = False, agents: list[dict] | None = None) -> dict[str, Any]:
    """Run the board for one question. Logs a board_runs row. Returns the run dict."""
    db.require_role(user, "viewer")
    t0 = time.time()
    language = language or user.get("language") or "en"
    agents = agents if agents is not None else db.get_doctor_agents(active_only=True)
    by_type = {a["agent_type"]: a for a in agents}
    advice_hits = detect_advice_request(query)
    advice_flagged = bool(advice_hits)
    calls: list[ModelCall] = []
    outputs: list[SpecialistOutput] = []
    errors: list[str] = []

    history = conversation_turns(conversation_id, user, query)
    turn = sum(1 for t in history if t["role"] == "board")
    previous = previous_open_items(patient_id, user)
    record = load_record(patient_id, user)
    index = record_index(record)
    known = _numbers(record_text(record))
    dropped: list[str] = []
    patient_first = ((record.header[0].split(":", 1)[1].strip() if record.header else "") or "").split(" ")[0] or None

    # 1. Triage
    assignments, reason, call, direct = triage(provider, query, index, agents, history=history, open_items=previous,
                                               force_all=force_all)
    if call:
        calls.append(call)
    chosen = [a["agent_type"] for a in assignments]

    # 2. Workup by role
    flags = pharmacist_flags(patient_id, user) if "pharmacist" in chosen else []
    findings: dict[str, SpecialistFindings] = {}
    for a in assignments:
        agent = by_type.get(a["agent_type"])
        if not agent:
            continue
        ctx = slice_record(record, agent)
        if a["agent_type"] == "pharmacist" and flags:
            ctx += "\n\nRECONCILIATION FLAGS (computed from the medication list):\n" + json.dumps(flags, ensure_ascii=False)
        try:
            import memory
            notes = memory.notes_block(patient_id, user, a["agent_type"], query)
        except Exception:
            notes = ""
        try:
            f, c = run_findings(provider, agent, a["question"], query, ctx, language, advice_flagged, notes=notes)
            dropped += ground_findings(f, known)
            findings[a["agent_type"]] = f
            calls.append(c)
        except ProviderError as e:
            errors.append(f"{a['agent_type']}: {e}")

    # 3. Discussion
    for a in assignments:
        t = a["agent_type"]
        if t not in findings:
            continue
        peers = [f for k, f in findings.items() if k != t]
        turn_out = None
        try:
            turn_out, c = run_discussion(provider, by_type[t], query, findings[t], peers, language, advice_flagged,
                                         history=history, open_items=previous)
            calls.append(c)
        except ProviderError as e:
            errors.append(f"discussion {t}: {e}")
        outputs.append(merge_specialist(findings[t], turn_out, a["question"]))

    # 4. Synthesis
    coordinator: CoordinatorOutput | None = None
    if direct and not outputs:
        text, _ = scrub_advice(direct)
        coordinator = CoordinatorOutput(headline=text, what_changed=[], key_observations=[], questions_by_doctor=[],
                                        missing_records=[], disagreements=[], declined_advice_request=advice_flagged,
                                        decline_note=REDIRECT if advice_flagged else None, open_items=[])
    elif outputs:
        try:
            coordinator, c = run_coordinator(provider, by_type.get("coordinator"), query, outputs, language, advice_flagged,
                                             history=history, open_items=previous, patient_first_name=patient_first)
            calls.append(c)
            dropped += ground_coordinator(coordinator, known)
        except ProviderError as e:
            errors.append(f"coordinator: {e}")
    if coordinator is None:
        coordinator = CoordinatorOutput(
            headline="The board could not complete this review." if errors else "No specialist produced output.",
            what_changed=[], key_observations=[], questions_by_doctor=[], missing_records=[], disagreements=[],
            declined_advice_request=advice_flagged, decline_note=REDIRECT if advice_flagged else None, open_items=[])
    # The redirect banner follows the question itself (the tested advice patterns), not
    # a model's own caution: a record question must not be answered with a refusal.
    if advice_flagged:
        coordinator.declined_advice_request = True
        coordinator.decline_note = REDIRECT
    else:
        coordinator.declined_advice_request = False
        coordinator.decline_note = None

    input_tokens = sum(c.input_tokens for c in calls)
    output_tokens = sum(c.output_tokens for c in calls)
    costs = [c.cost_usd for c in calls if c.cost_usd is not None]
    cost = round(sum(costs), 6) if costs else None
    status = "done" if not errors else ("partial" if outputs else "failed")
    duration_ms = int((time.time() - t0) * 1000)
    routing = {"agents": chosen, "reason": reason, "advice_patterns": advice_hits, "pharmacist_flags": flags,
               "assignments": assignments, "turn": turn, "direct": bool(direct), "previous_open_items": len(previous),
               "dropped_unverified": dropped}

    with db.db_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO board_runs (patient_id, user_id, conversation_id, query, language, routed_agents, specialist_outputs,
                                    coordinator_output, model_calls, input_tokens, output_tokens, estimated_cost_usd, status,
                                    error, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (patient_id, user["id"], conversation_id, query, language, json.dumps(routing),
              json.dumps([o.model_dump() for o in outputs]), json.dumps(coordinator.model_dump()),
              json.dumps([c.as_dict() for c in calls]), input_tokens, output_tokens, cost, status,
              "; ".join(errors) or None, duration_ms))
        run_id = cur.fetchone()["id"]
        _carry_items(coordinator, previous, run_id)
        cur.execute("UPDATE board_runs SET coordinator_output = %s WHERE id = %s", (json.dumps(coordinator.model_dump()), run_id))
        db.audit(cur, user, patient_id, "board.run", "board_run", run_id,
                 {"agents": chosen, "cost_usd": cost, "status": status, "declined": coordinator.declined_advice_request})

    # Every agent that took part remembers what it concluded, tied to this run,
    # so the family can read, correct or delete it on the Memory page.
    for o in outputs:
        parts = [o.summary.strip()] if o.summary else []
        for ob in o.observations[:3]:
            parts.append(ob.text.strip())
        memo = " ".join(p for p in parts if p)
        if memo and not o.declined_advice_request:
            db.save_doctor_memory(patient_id, user, o.agent_type, "review", memo[:900], importance=5, source_run_id=run_id)
    if coordinator.what_changed or coordinator.questions_by_doctor:
        memo = coordinator.headline + " " + " ".join(coordinator.what_changed[:3])
        db.save_doctor_memory(patient_id, user, "coordinator", "board_summary", memo[:900], importance=4, source_run_id=run_id)
    # Merge what was found into the shared profile (one small call; never fatal).
    consolidation = None
    if outputs and not coordinator.declined_advice_request:
        try:
            import memory
            consolidation = memory.consolidate(provider, patient_id, user, [o.model_dump() for o in outputs], run_id=run_id)
        except Exception as e:  # a memory failure must not lose the run
            consolidation = {"error": str(e)}

    return {
        "id": run_id, "patient_id": patient_id, "query": query, "language": language, "routed_agents": chosen,
        "routing_reason": reason, "assignments": assignments, "turn": turn, "direct": bool(direct),
        "advice_flagged": advice_flagged, "specialist_outputs": [o.model_dump() for o in outputs],
        "coordinator_output": coordinator.model_dump(), "model_calls": [c.as_dict() for c in calls],
        "input_tokens": input_tokens, "output_tokens": output_tokens, "estimated_cost_usd": cost, "status": status,
        "errors": errors, "duration_ms": duration_ms, "consolidation": consolidation,
    }


def list_runs(patient_id: int, user: dict, limit: int = 50):
    db.require_role(user, "viewer")
    with db.db_cursor() as cur:
        cur.execute("""SELECT id, query, language, routed_agents, coordinator_output, input_tokens, output_tokens,
                              estimated_cost_usd, status, error, duration_ms, created_at, user_id
                       FROM board_runs WHERE patient_id = %s ORDER BY created_at DESC, id DESC LIMIT %s""", (patient_id, limit))
        return cur.fetchall()


def get_run(run_id: int, patient_id: int, user: dict):
    db.require_role(user, "viewer")
    with db.db_cursor() as cur:
        cur.execute("SELECT * FROM board_runs WHERE id = %s AND patient_id = %s", (run_id, patient_id))
        return cur.fetchone()


def format_markdown(run: dict[str, Any], first_turn: bool | None = None) -> str:
    """Render a run as one reply in the conversation. The first reply carries
    the missing-records list and the footer; later replies read as chat."""
    if first_turn is None:
        first_turn = not run.get("turn")
    co = run.get("coordinator_output") or {}
    lines = []
    if co.get("declined_advice_request"):
        lines.append(f"> {co.get('decline_note') or REDIRECT}\n")
    lines.append(f"### {co.get('headline', '')}" if first_turn else co.get("headline", ""))
    if co.get("what_changed"):
        lines.append("\n**What changed**")
        lines += [f"- {s}" for s in co["what_changed"]]
    if co.get("key_observations"):
        lines.append("\n**What the records show**")
        for o in co["key_observations"]:
            refs = ", ".join(s.get("label", "") for s in o.get("sources", []) if s.get("label"))
            lines.append(f"- {o['text']}" + (f" _({refs})_" if refs else ""))
    groups = co.get("questions_by_doctor") or []
    earlier = 0
    if not first_turn and run.get("id"):
        older = {(i.get("text") or "").strip().lower() for i in co.get("open_items") or []
                 if i.get("since_run_id") and i.get("since_run_id") != run.get("id")}
        filtered = []
        for g in groups:
            qs = [x for x in g.get("questions", []) if x.strip().lower() not in older]
            earlier += len(g.get("questions", [])) - len(qs)
            if qs:
                filtered.append({"doctor": g["doctor"], "questions": qs})
        groups = filtered
    if groups:
        lines.append("\n**Questions to ask**")
        for q in groups:
            lines.append(f"- **{q['doctor']}**")
            lines += [f"  - {x}" for x in q["questions"]]
    if earlier:
        lines.append(f"\n_{earlier} earlier question{'s are' if earlier != 1 else ' is'} still open; the pre-visit brief lists them all._")
    if co.get("disagreements"):
        lines.append("\n**Where specialists differ**")
        lines += [f"- {s}" for s in co["disagreements"]]
    if first_turn and co.get("missing_records"):
        lines.append("\n**Missing records**")
        lines += [f"- {s}" for s in co["missing_records"]]
    if first_turn:
        agents = ", ".join(run.get("routed_agents") or []) or "chair only"
        cost = run.get("estimated_cost_usd")
        lines.append(f"\n_Specialists: {agents}. Tokens: {run.get('input_tokens', 0)} in, {run.get('output_tokens', 0)} out"
                     + (f". Estimated cost: ${cost:.4f}" if cost is not None else "") + "._")
    return "\n".join(lines)
