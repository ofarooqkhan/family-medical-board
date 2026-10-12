"""Board orchestration (items 8, 9, 10, 11): routing, structured specialist
output, pharmacist reconciliation flags, coordinator synthesis, scope
enforcement, and a cost log per run.

Basic first pass by design (HANDOFF 4.10). Every specialist returns the same
SpecialistOutput schema. The coordinator merges them into one brief. Nothing
here advises: prompts forbid it, advice-seeking queries are detected before
the run and answered with a redirect, and outputs are scrubbed for advice
phrasing afterwards.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel

import database as db
from providers import DEFAULT_MODELS, ModelCall, Part, Provider, ProviderError

MAX_SPECIALISTS = 5
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
# Schemas (item 8): one schema for every specialist
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


class CrossDomainFlag(BaseModel):
    item: str
    also_concerns: list[str]  # agent_types
    why: str


class SpecialistOutput(BaseModel):
    agent_type: str
    summary: str
    observations: list[Observation]
    questions_for_doctor: list[DoctorQuestion]
    missing_records: list[str]
    cross_domain_flags: list[CrossDomainFlag]
    declined_advice_request: bool
    decline_note: Optional[str] = None
    confidence: float


class QuestionsForDoctor(BaseModel):
    doctor: str
    questions: list[str]


class CoordinatorOutput(BaseModel):
    headline: str
    what_changed: list[str]
    key_observations: list[Observation]
    questions_by_doctor: list[QuestionsForDoctor]
    missing_records: list[str]
    disagreements: list[str]
    declined_advice_request: bool
    decline_note: Optional[str] = None


class RoutingDecision(BaseModel):
    agents: list[str]
    reason: str


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def build_context(patient_id: int, user: dict, max_chars: int = 24000) -> str:
    patient = db.get_patient(patient_id, user) or {}
    lines = [f"PATIENT: {patient.get('name')}"]
    for k in ("age", "sex", "date_of_birth", "weight_lbs", "allergies", "medical_history", "primary_doctor", "notes"):
        if patient.get(k):
            lines.append(f"{k.replace('_', ' ').title()}: {patient[k]}")

    try:
        import medications
        meds = medications.list_medications(patient_id, user, status="active")
    except Exception:
        meds = []
    lines.append("\nACTIVE MEDICATIONS (confirmed by the family from documents):")
    if meds:
        for m in meds:
            src = f" [doc {m['source_file_id']} p{m['source_page']}]" if m.get("source_file_id") else ""
            lines.append(f"- {m['name']} {m.get('dose') or ''} {m.get('frequency') or ''}; prescriber: {m.get('prescriber') or 'unknown'}; "
                         f"since {m.get('start_date') or 'unknown'}{src}")
    else:
        lines.append("- none recorded")
    if patient.get("medications"):
        lines.append(f"Free-text medication note: {patient['medications']}")

    lines.append("\nLATEST VALUES (with previous value where known):")
    latest = db.get_latest_vitals(patient_id, user)
    if latest:
        for v in latest:
            when = v["recorded_at"] or v["timestamp"]
            change = f", change {v['change_from_previous']:+.1f}" if v.get("change_from_previous") is not None else ""
            ref = f", ref {v['reference_range']}" if v.get("reference_range") else ""
            flag = f", {v['flag']}" if v.get("flag") and v["flag"] not in ("normal", "unknown") else ""
            src = f" [doc {v['source_file_id']} p{v['source_page']}]" if v.get("source_file_id") else ""
            lines.append(f"- {v['reading_type']}: {v['value']} {v['unit'] or ''} on {str(when)[:10]}{change}{ref}{flag}{src}")
    else:
        lines.append("- none recorded")

    lines.append("\nDOCUMENTS ON FILE (id, kind, date, status):")
    docs = db.list_documents(patient_id, user)
    for d in docs[:40]:
        lines.append(f"- doc {d['id']}: {d.get('document_kind') or d.get('file_type') or 'unknown'}, {d.get('document_date') or 'no date'}, "
                     f"{d['filename']}, {d['status']}")

    lines.append("\nEXTRACTED DOCUMENT CONTENT (most recent first):")
    budget = max_chars - sum(len(l) + 1 for l in lines)
    for d in docs[:15]:
        full = db.get_document(d["id"], patient_id, user)
        body = full.get("extraction") if full else None
        if body and isinstance(body, dict):
            chunk = json.dumps(body.get("document", body), ensure_ascii=False)[:3000]
        elif full and full.get("extracted_text"):
            chunk = full["extracted_text"][:1500]
        else:
            continue
        entry = f"--- doc {d['id']} ({d.get('document_kind')}, {d.get('document_date')}) ---\n{chunk}"
        if len(entry) > budget:
            break
        lines.append(entry)
        budget -= len(entry) + 1

    mem = db.get_doctor_memories(patient_id, user, limit=15)
    if mem:
        lines.append("\nNOTES FROM EARLIER REVIEWS:")
        for m in mem:
            lines.append(f"- [{m['doctor_type']}] {m['content'][:300]}")

    try:
        import appointments
        upcoming = appointments.list_appointments(patient_id, user, upcoming_only=True, limit=5)
        if upcoming:
            lines.append("\nUPCOMING APPOINTMENTS:")
            for a in upcoming:
                lines.append(f"- {a['starts_at']}: {a['title']} with {a.get('doctor_name') or a.get('specialty') or 'unknown'}")
    except Exception:
        pass
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Routing (item 10)
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
    """Pick the specialists for this query. Keyword match first, model second."""
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


# ---------------------------------------------------------------------------
# Specialists and coordinator
# ---------------------------------------------------------------------------

def run_specialist(provider: Provider, agent: dict, query: str, context: str, language: str,
                   advice_flagged: bool, model: str | None = None) -> tuple[SpecialistOutput, ModelCall]:
    model = model or agent.get("model") or DEFAULT_MODELS["specialist"]
    system = agent.get("system_prompt") or agent.get("backstory") or ""
    system += (f"\nSet agent_type to '{agent['agent_type']}'. Write summary and observation text in "
               f"{'Urdu' if language == 'ur' else 'English'}. Cite sources using the doc ids and page numbers from the record. "
               "Flag anything that another specialist should also see in cross_domain_flags.")
    if advice_flagged:
        system += ("\nThe question asks for advice. Set declined_advice_request to true, put one sentence in decline_note "
                   "that points to the treating doctor, and still summarize what the records show on the topic.")
    user_text = f"RECORDS:\n{context}\n\nFAMILY'S QUESTION: {query or 'Please review everything in your domain.'}"
    out, call = provider.structured(model=model, system=system, parts=[Part.of_text(user_text)],
                                    schema=SpecialistOutput, purpose=f"specialist:{agent['agent_type']}", max_tokens=3000)
    out.agent_type = agent["agent_type"]
    out.summary, _ = scrub_advice(out.summary)
    for o in out.observations:
        o.text, _ = scrub_advice(o.text)
    return out, call


def run_coordinator(provider: Provider, agent: dict | None, query: str, outputs: list[SpecialistOutput], language: str,
                    advice_flagged: bool, model: str | None = None) -> tuple[CoordinatorOutput, ModelCall]:
    model = model or (agent or {}).get("model") or DEFAULT_MODELS["coordinator"]
    system = (agent or {}).get("system_prompt") or (
        "You are the care coordinator. Merge the specialists' structured outputs into one brief for the family. "
        "Never give medical advice.")
    system += (f"\nWrite in {'Urdu' if language == 'ur' else 'English'}. Merge duplicate questions, group them by the doctor "
               "who should hear them, keep every source reference, and list where specialists disagree.")
    if advice_flagged:
        system += "\nThe family asked for advice. Set declined_advice_request true and decline_note to a one-sentence redirect."
    payload = json.dumps([o.model_dump() for o in outputs], ensure_ascii=False)
    user_text = f"FAMILY'S QUESTION: {query or 'full review'}\n\nSPECIALIST OUTPUTS (JSON):\n{payload}"
    out, call = provider.structured(model=model, system=system, parts=[Part.of_text(user_text)],
                                    schema=CoordinatorOutput, purpose="coordinator", max_tokens=3000)
    out.headline, _ = scrub_advice(out.headline)
    out.what_changed = [scrub_advice(s)[0] for s in out.what_changed]
    for o in out.key_observations:
        o.text, _ = scrub_advice(o.text)
    return out, call


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

    context = build_context(patient_id, user)
    chosen, reason, call = route(provider, query, context, agents, force_all=force_all)
    if call:
        calls.append(call)

    flags = pharmacist_flags(patient_id, user) if "pharmacist" in chosen else []
    for agent_type in chosen:
        agent = by_type.get(agent_type)
        if not agent:
            continue
        ctx = context
        if agent_type == "pharmacist" and flags:
            ctx += "\n\nRECONCILIATION FLAGS (computed from the medication list):\n" + json.dumps(flags, ensure_ascii=False)
        try:
            out, c = run_specialist(provider, agent, query, ctx, language, advice_flagged)
            outputs.append(out)
            calls.append(c)
        except ProviderError as e:
            errors.append(f"{agent_type}: {e}")

    coordinator: CoordinatorOutput | None = None
    if outputs:
        try:
            coordinator, c = run_coordinator(provider, by_type.get("coordinator"), query, outputs, language, advice_flagged)
            calls.append(c)
        except ProviderError as e:
            errors.append(f"coordinator: {e}")
    if coordinator is None:
        coordinator = CoordinatorOutput(
            headline="The board could not complete this review." if errors else "No specialist produced output.",
            what_changed=[], key_observations=[], questions_by_doctor=[], missing_records=[], disagreements=[],
            declined_advice_request=advice_flagged, decline_note=REDIRECT if advice_flagged else None)
    if advice_flagged or any(o.declined_advice_request for o in outputs):
        coordinator.declined_advice_request = True
        coordinator.decline_note = coordinator.decline_note or REDIRECT
        if REDIRECT.split(".")[0].lower() not in (coordinator.decline_note or "").lower():
            coordinator.decline_note = f"{REDIRECT} {coordinator.decline_note or ''}".strip()

    input_tokens = sum(c.input_tokens for c in calls)
    output_tokens = sum(c.output_tokens for c in calls)
    costs = [c.cost_usd for c in calls if c.cost_usd is not None]
    cost = round(sum(costs), 6) if costs else None
    status = "done" if not errors else ("partial" if outputs else "failed")
    duration_ms = int((time.time() - t0) * 1000)

    with db.db_cursor(commit=True) as cur:
        cur.execute("""
            INSERT INTO board_runs (patient_id, user_id, conversation_id, query, language, routed_agents, specialist_outputs,
                                    coordinator_output, model_calls, input_tokens, output_tokens, estimated_cost_usd, status,
                                    error, duration_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (patient_id, user["id"], conversation_id, query, language,
              json.dumps({"agents": chosen, "reason": reason, "advice_patterns": advice_hits, "pharmacist_flags": flags}),
              json.dumps([o.model_dump() for o in outputs]), json.dumps(coordinator.model_dump()),
              json.dumps([c.as_dict() for c in calls]), input_tokens, output_tokens, cost, status,
              "; ".join(errors) or None, duration_ms))
        run_id = cur.fetchone()["id"]
        db.audit(cur, user, patient_id, "board.run", "board_run", run_id,
                 {"agents": chosen, "cost_usd": cost, "status": status, "declined": coordinator.declined_advice_request})

    if coordinator.what_changed or coordinator.questions_by_doctor:
        memo = coordinator.headline + " " + " ".join(coordinator.what_changed[:3])
        db.save_doctor_memory(patient_id, user, "coordinator", "board_summary", memo[:900], importance=4)

    return {
        "id": run_id, "patient_id": patient_id, "query": query, "language": language, "routed_agents": chosen,
        "routing_reason": reason, "advice_flagged": advice_flagged, "specialist_outputs": [o.model_dump() for o in outputs],
        "coordinator_output": coordinator.model_dump(), "model_calls": [c.as_dict() for c in calls],
        "input_tokens": input_tokens, "output_tokens": output_tokens, "estimated_cost_usd": cost, "status": status,
        "errors": errors, "duration_ms": duration_ms,
    }


def list_runs(patient_id: int, user: dict, limit: int = 50):
    db.require_role(user, "viewer")
    with db.db_cursor() as cur:
        cur.execute("""SELECT id, query, language, routed_agents, coordinator_output, input_tokens, output_tokens,
                              estimated_cost_usd, status, error, duration_ms, created_at, user_id
                       FROM board_runs WHERE patient_id = %s ORDER BY created_at DESC LIMIT %s""", (patient_id, limit))
        return cur.fetchall()


def get_run(run_id: int, patient_id: int, user: dict):
    db.require_role(user, "viewer")
    with db.db_cursor() as cur:
        cur.execute("SELECT * FROM board_runs WHERE id = %s AND patient_id = %s", (run_id, patient_id))
        return cur.fetchone()


def format_markdown(run: dict[str, Any]) -> str:
    """Render a run for chat display."""
    co = run.get("coordinator_output") or {}
    lines = []
    if co.get("declined_advice_request"):
        lines.append(f"> {co.get('decline_note') or REDIRECT}\n")
    lines.append(f"### {co.get('headline', '')}")
    if co.get("what_changed"):
        lines.append("\n**What changed**")
        lines += [f"- {s}" for s in co["what_changed"]]
    if co.get("key_observations"):
        lines.append("\n**What the records show**")
        for o in co["key_observations"]:
            refs = ", ".join(s.get("label", "") for s in o.get("sources", []) if s.get("label"))
            lines.append(f"- {o['text']}" + (f" _({refs})_" if refs else ""))
    if co.get("questions_by_doctor"):
        lines.append("\n**Questions to ask**")
        for q in co["questions_by_doctor"]:
            lines.append(f"- **{q['doctor']}**")
            lines += [f"  - {x}" for x in q["questions"]]
    if co.get("missing_records"):
        lines.append("\n**Missing records**")
        lines += [f"- {s}" for s in co["missing_records"]]
    if co.get("disagreements"):
        lines.append("\n**Where specialists differ**")
        lines += [f"- {s}" for s in co["disagreements"]]
    agents = ", ".join(run.get("routed_agents") or [])
    cost = run.get("estimated_cost_usd")
    lines.append(f"\n_Specialists: {agents}. Tokens: {run.get('input_tokens', 0)} in, {run.get('output_tokens', 0)} out"
                 + (f". Estimated cost: ${cost:.4f}" if cost is not None else "") + "._")
    return "\n".join(lines)
