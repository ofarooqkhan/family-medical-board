"""Board tests: scope enforcement prompt set (no database) and a full run on the fake provider (database)."""
import os
import re

import pytest

import board
from board import (CoordinatorOutput, DiscussionTurn, RoutingDecision, SpecialistFindings, TriageDecision,
                   detect_advice_request, scrub_advice)
from providers import FakeProvider

ADVICE_SEEKING = [
    "Should I increase my father's insulin tonight?",
    "How much metformin should we give him?",
    "Can she take ibuprofen with her blood thinner?",
    "What dose of amlodipine should he start on?",
    "Should we stop the statin because of the muscle pain?",
    "Is it safe to skip the water pill on travel days?",
    "Which antibiotic is best for her UTI?",
    "Can you recommend a supplement for his kidneys?",
    "Please suggest a medicine for the constipation.",
    "Should my mother take paracetamol or ibuprofen for the back pain?",
    "Increase the dose of levothyroxine?",
    "Can we double the gabapentin at night?",
    "Does he need antibiotics for this cough?",
    "What medication should we give for the dizziness?",
    "Is it okay to give her the sleeping tablet every night?",
    "How many units of insulin for a sugar of 300?",
    "Can we treat this swelling at home without a doctor?",
    "Should we switch him to a different blood pressure medicine?",
    "Is there a natural cure instead of the medication?",
    "Reduce her insulin since the sugars are low?",
]

RECORD_QUESTIONS = [
    "What dose of metformin is he currently on according to the latest prescription?",
    "Has her creatinine changed since the last report?",
    "List the medications from the two prescriptions and who prescribed each.",
    "When was the last HbA1c and what was the value?",
    "What did the MRI report say compared with the earlier one?",
    "Which questions should we ask the cardiologist on Tuesday?",
    "Summarize everything from the last month.",
    "Are there any medications prescribed by more than one doctor?",
]


@pytest.mark.parametrize("query", ADVICE_SEEKING)
def test_advice_seeking_queries_are_detected(query):
    assert detect_advice_request(query), query


@pytest.mark.parametrize("query", RECORD_QUESTIONS)
def test_record_questions_are_not_flagged(query):
    assert detect_advice_request(query) == [], query


def test_scrub_advice_removes_advice_sentences_only():
    text = ("Creatinine rose from 1.1 to 1.4 between May and September. You should increase the dose of the water pill. "
            "The lab flagged potassium as high. I recommend stopping the supplement.")
    cleaned, removed = scrub_advice(text)
    assert removed == 2
    assert "Creatinine rose" in cleaned and "potassium" in cleaned
    assert "recommend" not in cleaned and "increase the dose" not in cleaned
    assert scrub_advice("You should take 2 tablets.")[0] == board.REDIRECT


def _agents():
    return [
        {"agent_type": "cardiologist", "display_name": "Cardiologist", "system_prompt": "cardio prompt. You never give medical advice.",
         "domains": ["heart", "blood pressure", "bp"], "is_active": True, "model": None},
        {"agent_type": "nephrologist", "display_name": "Nephrologist", "system_prompt": "renal prompt.",
         "domains": ["kidney", "creatinine", "egfr"], "is_active": True, "model": None},
        {"agent_type": "pharmacist", "display_name": "Pharmacist", "system_prompt": "pharm prompt.",
         "domains": ["medication", "drug", "dose"], "is_active": True, "model": None},
        {"agent_type": "general_duty", "display_name": "General Duty", "system_prompt": "gd prompt.",
         "domains": ["fever", "general"], "is_active": True, "model": None},
        {"agent_type": "coordinator", "display_name": "Coordinator", "system_prompt": "coord prompt.", "domains": [],
         "is_active": True, "model": None},
    ]


def test_keyword_routing_prefers_query_hits_and_adds_pharmacist_for_medication_questions():
    agents = _agents()
    chosen, reason, call = board.route(FakeProvider(), "Has his creatinine changed and is the dose of the kidney medication recorded?", "", agents)
    assert set(chosen) == {"nephrologist", "pharmacist"}
    assert call is None and reason == "keyword match"


def test_routing_falls_back_to_model_then_general_duty():
    fake = FakeProvider(responses={RoutingDecision: {"agents": ["cardiologist", "not-an-agent"], "reason": "chest"}})
    chosen, reason, call = board.route(fake, "He felt faint after lunch", "", _agents())
    assert chosen == ["cardiologist"] and call is not None and call.purpose == "route"
    chosen, reason, _ = board.route(FakeProvider(responses={RoutingDecision: {"agents": [], "reason": "?"}}), "He felt faint", "", _agents())
    assert chosen == ["general_duty"] and "general duty" in reason


def test_keyword_routing_matches_whole_words_only():
    agents = _agents() + [{"agent_type": "gastroenterologist", "display_name": "GI", "system_prompt": "", "domains": ["ast", "alt", "liver"],
                           "is_active": True, "model": None}]
    assert board.keyword_route("What changed since the last visit?", "", agents) == []
    assert board.keyword_route("His AST and ALT were high", "", agents) == ["gastroenterologist"]
    assert board.keyword_route("heart rate 70 bpm", "", agents) == ["cardiologist"]
    assert board.keyword_route("the bp was 150", "", agents) == ["cardiologist"]


def test_full_review_routes_to_every_specialist():
    chosen, reason, call = board.route(FakeProvider(), "Please do a full review", "", _agents())
    assert set(chosen) == {"cardiologist", "nephrologist", "pharmacist", "general_duty"} and call is None


# ---------------------------------------------------------------------------
# Canned board: triage picks by the question, each specialist reports one
# finding, the discussion turns it into one question, the chair merges.
# ---------------------------------------------------------------------------

def _triage_response(parts):
    text = parts[0].text
    menu = re.findall(r"^- ([a-z_]+):", text.split("SPECIALISTS AVAILABLE:")[1].split("CONVERSATION")[0], re.M)
    q = text.rsplit("FAMILY'S QUESTION:", 1)[1].strip()
    if "full review" in q.lower():
        return {"assignments": [{"agent_type": t, "question": f"Review everything in your domain for: {q}"} for t in menu],
                "direct_answer": None, "reason": "demo triage: full review"}
    if "documents" in q.lower():
        return {"assignments": [], "direct_answer": "Two documents are on file: a BP log and a lab report.", "reason": "record lookup"}
    chosen = ["nephrologist"] if re.search(r"creatinine|kidney|egfr", q, re.I) and "nephrologist" in menu else ["cardiologist"]
    return {"assignments": [{"agent_type": t, "question": f"What do the records show about {t}'s domain for: {q}"} for t in chosen],
            "direct_answer": None, "reason": "demo triage"}


def _findings_response(parts):
    text = parts[0].text
    return {
        "agent_type": "cardiologist", "summary": "BP readings rose over the summer. You should increase the dose.",
        "observations": [{"text": "Systolic 150 on 2026-09-01 vs 132 in May.", "category": "trend",
                          "sources": [{"file_id": 3, "page": 1, "label": "BP log p1"}], "confidence": 0.8}],
        "missing_records": ["No ECG on file"], "cross_domain_flags": [{"item": "amlodipine", "also_concerns": ["nephrologist"], "why": "swelling"}],
        "declined_advice_request": "ADVICE" in text, "decline_note": None, "confidence": 0.8,
    }


def _discussion_response(parts):
    text = parts[0].text
    peers = [] if "No other specialist took part" in text else [{"to": "nephrologist", "stance": "agree", "text": "The creatinine trend fits."}]
    return {"agent_type": "cardiologist", "responses": peers, "added_observations": [],
            "questions_for_doctor": [{"question": "Is the rise in systolic pressure expected?", "for_doctor": "cardiologist",
                                      "why": "trend", "priority": "medium", "based_on": "Systolic 150 on 2026-09-01 vs 132 in May."}],
            "confidence": 0.8}


COORD = {
    "headline": "Blood pressure has risen since May. We recommend stopping the salt.",
    "what_changed": ["Systolic pressure up by 18 points since May."],
    "key_observations": [{"text": "Systolic 150 on 2026-09-01.", "category": "value",
                          "sources": [{"file_id": 3, "page": 1, "label": "BP log p1"}], "confidence": 0.8}],
    "questions_by_doctor": [{"doctor": "Cardiologist", "questions": ["Is the rise in systolic pressure expected?"]}],
    "missing_records": ["No ECG on file"], "disagreements": [], "declined_advice_request": False, "decline_note": None,
    "open_items": None,
}

RESPONSES = {TriageDecision: _triage_response, SpecialistFindings: _findings_response, DiscussionTurn: _discussion_response,
             CoordinatorOutput: COORD}


def _fake(overrides=None):
    return FakeProvider(responses={**RESPONSES, **(overrides or {})})


def test_triage_assigns_one_question_per_specialist_and_falls_back_to_keywords_when_the_chair_fails():
    assignments, reason, call, direct = board.triage(_fake(), "Has his creatinine changed?", "", _agents())
    assert [a["agent_type"] for a in assignments] == ["nephrologist"] and "creatinine" in assignments[0]["question"]
    assert call.purpose == "triage" and direct is None
    # A record lookup is answered by the chair without convening anyone.
    assignments, reason, call, direct = board.triage(_fake(), "Which documents are on file?", "", _agents())
    assert assignments == [] and direct.startswith("Two documents")
    # Chair unavailable: keyword routing takes over, every specialist gets the family's question verbatim.
    assignments, reason, call, direct = board.triage(FakeProvider(), "Has his creatinine changed and is the dose recorded?", "", _agents())
    assert {a["agent_type"] for a in assignments} == {"nephrologist", "pharmacist"} and "chair failed" in reason
    assert all(a["question"].startswith("Has his creatinine") for a in assignments)
    # Unknown agents from the chair are dropped; nothing left means general duty.
    bad = _fake({TriageDecision: {"assignments": [{"agent_type": "ghost", "question": "?"}], "direct_answer": None, "reason": "x"}})
    assignments, reason, _, _ = board.triage(bad, "He felt faint", "", _agents())
    assert [a["agent_type"] for a in assignments] == ["general_duty"] and "general duty" in reason
    # Full review convenes everyone without a model call.
    assignments, reason, call, _ = board.triage(_fake(), "x", "", _agents(), force_all=True)
    assert len(assignments) == 4 and call is None


def test_slice_record_gives_each_role_its_own_documents_and_everyone_the_index():
    rec = board.Record(header=["PATIENT: P"], meds=["ACTIVE MEDICATIONS:", "- Metformin"], vitals=["LATEST VALUES:", "- creatinine: 1.4"],
                       doc_index=["DOCUMENTS ON FILE:", "- doc 1", "- doc 2", "- doc 3", "- doc 4"],
                       docs=[{"id": 1, "kind": "lab_report", "date": "2026-09-05", "filename": "labs.pdf", "text": "creatinine 1.4 eGFR 48"},
                             {"id": 2, "kind": "imaging_report", "date": "2026-08-20", "filename": "ct.pdf", "text": "CT head no acute change"},
                             {"id": 3, "kind": "prescription", "date": "2026-08-01", "filename": "rx.jpg", "text": "amlodipine 5 mg"},
                             {"id": 4, "kind": "discharge_summary", "date": "2026-07-01", "filename": "dc.pdf", "text": "admitted for a fall"}])
    agents = {a["agent_type"]: a for a in _agents()}
    agents["radiologist"] = {"agent_type": "radiologist", "domains": ["ct", "mri", "scan"]}
    nephro = board.slice_record(rec, agents["nephrologist"])
    assert "doc 1 (lab_report" in nephro and "doc 4 (discharge_summary" in nephro
    assert "doc 2 (imaging_report" not in nephro and "doc 3 (prescription" not in nephro
    assert "- creatinine: 1.4" in nephro and "- doc 2" in nephro  # index and values are shared
    pharm = board.slice_record(rec, agents["pharmacist"])
    assert "doc 3 (prescription" in pharm and "doc 2 (imaging_report" not in pharm
    radio = board.slice_record(rec, agents["radiologist"])
    assert "doc 2 (imaging_report" in radio and "doc 1 (lab_report" not in radio
    everything = board.slice_record(rec, agents["general_duty"])
    assert all(f"doc {i} (" in everything for i in (1, 2, 3, 4))
    # Nothing in the domain: the most recent documents are shown with a note, so the specialist can say what is missing.
    eye = board.slice_record(rec, {"agent_type": "ophthalmologist", "domains": ["eye", "retina"]})
    assert "doc 4 (discharge_summary" in eye and "doc 1 (lab_report" not in eye  # a discharge summary is read by every role
    no_match = board.Record(header=rec.header, meds=rec.meds, vitals=rec.vitals, doc_index=rec.doc_index, docs=rec.docs[:3])
    eye = board.slice_record(no_match, {"agent_type": "ophthalmologist", "domains": ["eye", "retina"]})
    assert "No document on file matches your domain" in eye and "doc 1 (lab_report" in eye
    assert len(board.slice_record(rec, agents["general_duty"], max_chars=400)) <= 400 + 200


def test_format_markdown_first_reply_carries_the_footer_and_missing_records_later_replies_read_as_chat():
    run = {"coordinator_output": COORD, "routed_agents": ["cardiologist"], "input_tokens": 10, "output_tokens": 5, "estimated_cost_usd": 0.001}
    first = board.format_markdown(run)
    assert first.startswith("### ") and "Missing records" in first and "_Specialists: cardiologist" in first
    later = board.format_markdown({**run, "turn": 1})
    assert not later.startswith("###") and "Missing records" not in later and "Specialists:" not in later
    assert "Questions to ask" in later and "Blood pressure has risen" in later


pytestmark_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@pytestmark_db
def test_run_board_logs_cost_and_scrubs_advice(accounts):
    import database as db
    user = accounts["editor"]
    pid = db.create_patient(user, "Parent One", age=81)
    db.save_vital_reading(pid, user, "blood_pressure_systolic", 150, "mmHg")
    fake = _fake()
    run = board.run_board(pid, user, "How has his blood pressure changed?", fake)
    assert run["routed_agents"] == ["cardiologist"] and run["assignments"][0]["question"].startswith("What do the records show")
    assert run["status"] == "done" and run["advice_flagged"] is False and run["turn"] == 0
    assert run["estimated_cost_usd"] is not None and run["input_tokens"] > 0
    assert [c["purpose"] for c in run["model_calls"]] == ["triage", "specialist:cardiologist", "discussion:cardiologist", "coordinator"]
    assert "recommend" not in run["coordinator_output"]["headline"]
    assert "You should" not in run["specialist_outputs"][0]["summary"]
    assert run["specialist_outputs"][0]["assigned_question"] == run["assignments"][0]["question"]
    assert run["specialist_outputs"][0]["questions_for_doctor"][0]["based_on"].startswith("Systolic 150")
    assert run["coordinator_output"]["key_observations"][0]["sources"][0]["file_id"] == 3
    # The chair gave no plan state, so the kept question became the open item, stamped with this run.
    assert run["coordinator_output"]["open_items"] == [{"text": "Is the rise in systolic pressure expected?", "doctor": "Cardiologist",
                                                        "status": "open", "since_run_id": run["id"]}]
    # The specialist worked from its slice: the chair's question and the records, not the whole conversation.
    spec = next(c for c in fake.calls if c["purpose"] == "specialist:cardiologist")
    assert "THE CHAIR'S QUESTION TO YOU" in spec["parts"][0].text and "LATEST VALUES" in spec["parts"][0].text
    stored = board.get_run(run["id"], pid, user)
    assert stored["estimated_cost_usd"] == pytest.approx(run["estimated_cost_usd"])
    assert stored["routed_agents"]["reason"] == "demo triage" and stored["routed_agents"]["assignments"] == run["assignments"]
    assert stored["coordinator_output"]["open_items"][0]["since_run_id"] == run["id"]
    assert any(r["action"] == "board.run" for r in db.get_audit_log(pid))
    assert board.get_run(run["id"], pid + 1, user) is None
    md = board.format_markdown(run)
    assert "Questions to ask" in md and "Cardiologist" in md


@pytestmark_db
def test_run_board_declines_advice_and_still_summarizes(accounts):
    import database as db
    user = accounts["viewer"]
    pid = db.create_patient(accounts["editor"], "Parent One")
    # Readings the coordinator's numbers can be checked against (132 then 150: a rise of 18).
    db.save_vital_reading(pid, accounts["editor"], "blood_pressure_systolic", 132, "mmHg")
    db.save_vital_reading(pid, accounts["editor"], "blood_pressure_systolic", 150, "mmHg")
    fake = _fake()
    run = board.run_board(pid, user, "Should we increase his blood pressure tablet?", fake)
    assert run["advice_flagged"] is True
    co = run["coordinator_output"]
    assert co["declined_advice_request"] is True
    assert board.REDIRECT.split(".")[0] in co["decline_note"]
    assert co["what_changed"]  # the record summary is still there
    md = board.format_markdown(run)
    assert md.startswith("> This board does not give medical advice")
    # The specialist saw the advice instruction.
    assert any("asks for advice" in c["system"] for c in fake.calls if c["purpose"].startswith("specialist"))
    # The chair was told too, so it assigns record questions instead of passing the advice question on.
    triage_text = next(c for c in fake.calls if c["purpose"] == "triage")["parts"][0].text
    assert "asks for advice" in triage_text and "covers every part" in next(c for c in fake.calls if c["purpose"] == "triage")["system"]


@pytestmark_db
def test_run_board_survives_a_failing_specialist(accounts):
    import database as db
    user = accounts["editor"]
    pid = db.create_patient(user, "Parent One")
    calls = {"n": 0}

    def flaky(parts):
        calls["n"] += 1
        if calls["n"] == 1:
            from providers import ProviderError
            raise ProviderError("boom")
        return _findings_response(parts)

    fake = _fake({SpecialistFindings: flaky})
    run = board.run_board(pid, user, "full review", fake, agents=_agents())
    assert run["status"] == "partial" and run["errors"]
    assert len(run["specialist_outputs"]) == 3 and run["routed_agents"] == ["cardiologist", "nephrologist", "pharmacist", "general_duty"]
    # In discussion each remaining specialist saw the other two.
    disc = [c for c in fake.calls if c["purpose"].startswith("discussion:")]
    assert len(disc) == 3 and all("OTHER SPECIALISTS' FINDINGS" in c["parts"][0].text for c in disc)
    assert all(o["discussion"] for o in run["specialist_outputs"])
    # The pharmacist got the reconciliation flags block only if there were flags; the index always.
    pharm = next(c for c in fake.calls if c["purpose"] == "specialist:pharmacist")
    assert "DOCUMENTS ON FILE" in pharm["parts"][0].text


@pytestmark_db
def test_conversation_continues_and_plan_state_carries_between_runs(accounts):
    import database as db
    user = accounts["editor"]
    pid = db.create_patient(user, "Parent One")
    conv = db.create_conversation(user, pid, "BP")
    fake = _fake()
    db.save_chat_message(pid, conv, user, "user", "How has his blood pressure changed?")
    first = board.run_board(pid, user, "How has his blood pressure changed?", fake, conversation_id=conv)
    db.save_chat_message(pid, conv, user, "assistant", board.format_markdown(first), responding_doctor="coordinator")
    assert first["turn"] == 0
    triage_call = next(c for c in fake.calls if c["purpose"] == "triage")
    assert "this is the first question" in triage_call["parts"][0].text and "OPEN ITEMS FROM THE LAST REVIEW: none" in triage_call["parts"][0].text

    fake.calls.clear()
    db.save_chat_message(pid, conv, user, "user", "And the kidneys?")
    second = board.run_board(pid, user, "And the kidneys?", fake, conversation_id=conv)
    assert second["turn"] == 1
    md = board.format_markdown(second)
    # A follow-up reads as chat: no first-reply sections, and a question already raised
    # in the first turn is not repeated; the reply says it is still open.
    assert "Missing records" not in md and "_Specialists:" not in md
    assert "Questions to ask" not in md and "1 earlier question is still open" in md
    # The chair, the discussion and the synthesis all saw the earlier turns and the open item from the first run.
    assert second["routed_agents"] == ["nephrologist"]  # "the kidneys" was read against the earlier turn
    for purpose in ("triage", "discussion:nephrologist", "coordinator"):
        text = next(c for c in fake.calls if c["purpose"] == purpose)["parts"][0].text
        assert "[family] How has his blood pressure changed?" in text, purpose
        assert "[board]" in text and "Is the rise in systolic pressure expected?" in text, purpose
        assert "Specialists: cardiologist" not in text  # the footer is stripped from the history
    # The findings call is kept small: records and the chair's question, no history.
    spec = next(c for c in fake.calls if c["purpose"] == "specialist:nephrologist")["parts"][0].text
    assert "CONVERSATION SO FAR" not in spec
    # Plan state: the item from run one is still open and keeps its origin; the chair added nothing new that differs.
    items = second["coordinator_output"]["open_items"]
    assert [(i["text"], i["status"], i["since_run_id"]) for i in items] == [("Is the rise in systolic pressure expected?", "open", first["id"])]
    assert board.previous_open_items(pid, user) == items

    # A chair that re-lists the item without its origin does not make it look new.
    relisted = {**COORD, "open_items": [{"text": "Is the rise in systolic pressure expected?", "doctor": "Cardiologist",
                                         "status": "open", "since_run_id": None}]}
    again = board.run_board(pid, user, "Anything else on blood pressure?", _fake({CoordinatorOutput: relisted}), conversation_id=conv)
    assert [i["since_run_id"] for i in again["coordinator_output"]["open_items"]] == [first["id"]]

    # A chair that marks the item answered closes it, and the next run no longer carries it.
    closed = {**COORD, "questions_by_doctor": [{"doctor": "Nephrologist", "questions": ["Is the eGFR trend expected?"]}],
              "open_items": [{"text": "Is the rise in systolic pressure expected?", "doctor": "Cardiologist", "status": "answered",
                              "since_run_id": first["id"]},
                             {"text": "Is the eGFR trend expected?", "doctor": "Nephrologist", "status": "open", "since_run_id": None}]}
    third = board.run_board(pid, user, "Did the cardiologist answer?", _fake({CoordinatorOutput: closed}), conversation_id=conv)
    assert [(i["status"], i["since_run_id"]) for i in third["coordinator_output"]["open_items"]] == [("answered", first["id"]), ("open", third["id"])]
    assert [i["text"] for i in board.previous_open_items(pid, user)] == ["Is the eGFR trend expected?"]
    import brief
    assert [q["question"] for q in brief.build_brief(pid, user)["questions"]] == ["Is the eGFR trend expected?"]
    assert brief.build_brief(pid, user)["questions"][0]["run_id"] == third["id"]


@pytestmark_db
def test_chair_answers_a_record_lookup_without_convening_the_board(accounts):
    import database as db
    user = accounts["editor"]
    pid = db.create_patient(user, "Parent One")
    fake = _fake()
    run = board.run_board(pid, user, "Which documents are on file?", fake)
    assert run["direct"] is True and run["routed_agents"] == [] and run["specialist_outputs"] == []
    assert run["status"] == "done" and [c["purpose"] for c in run["model_calls"]] == ["triage"]
    assert run["coordinator_output"]["headline"].startswith("Two documents are on file")
    assert "chair only" in board.format_markdown(run)
    assert db.list_doctor_memories(pid, user) == []  # nothing was concluded, nothing is remembered


@pytestmark_db
def test_every_specialist_remembers_its_review_and_memories_are_editable(accounts):
    import database as db
    admin, editor, viewer = accounts["admin"], accounts["editor"], accounts["viewer"]
    pid = db.create_patient(admin, "Parent One")
    provider = _fake()
    run = board.run_board(pid, editor, "What do the records show about blood pressure?", provider)
    assert run["status"] == "done" and run["routed_agents"]
    mems = db.list_doctor_memories(pid, viewer)
    by_agent = {m["doctor_type"] for m in mems}
    for agent in run["routed_agents"]:
        assert agent in by_agent, (agent, by_agent)
    assert "coordinator" in by_agent
    assert all(m["source_run_id"] == run["id"] for m in mems if m["memory_type"] in ("review", "board_summary"))
    assert all(m["created_by"] == editor["id"] for m in mems)
    first = next(m for m in mems if m["memory_type"] == "review")
    with pytest.raises(PermissionError):
        db.update_doctor_memory(first["id"], pid, viewer, "x")
    db.update_doctor_memory(first["id"], pid, editor, "Corrected by the family.", 9)
    fresh = next(m for m in db.list_doctor_memories(pid, viewer) if m["id"] == first["id"])
    assert fresh["content"] == "Corrected by the family." and fresh["importance"] == 9 and fresh["updated_by"] == editor["id"]
    other = db.create_patient(admin, "Parent Two")
    with pytest.raises(PermissionError):
        db.delete_doctor_memory(first["id"], other, editor)
    db.delete_doctor_memory(first["id"], pid, editor)
    assert first["id"] not in {m["id"] for m in db.list_doctor_memories(pid, viewer)}
    with pytest.raises(PermissionError):
        db.clear_doctor_memories(pid, editor)
    n = db.clear_doctor_memories(pid, admin, "coordinator")
    assert n >= 1 and "coordinator" not in {m["doctor_type"] for m in db.list_doctor_memories(pid, viewer)}
    actions = [r["action"] for r in db.get_audit_log(pid)]
    assert {"memory.update", "memory.delete", "memory.clear"} <= set(actions)



def test_numbers_not_in_the_record_are_dropped_before_the_family_or_memory_sees_them():
    known = board._numbers("lab_potassium: 4.6 mmol/L on 2026-09-05, ref 3.5 - 5.1; creatinine 1.3 ref 0.6 - 1.2; "
                           "Metformin 500 mg; glucose 142.0 ref 70 - 100")
    assert board.ungrounded("The potassium level is 4.5 mmol/L, within 3.5 - 5.0.", known) == [4.5, 5.0]
    assert board.ungrounded("Potassium 4.6 mmol/L (3.5 - 5.1); glucose 142 mg/dL.", known) == []
    assert board.ungrounded("Review in 3 months; HbA1c result dated 2026-09-05; 2 tablets.", known) == []
    f = SpecialistFindings.model_validate({
        "agent_type": "nephrologist", "summary": "Potassium 4.5, creatinine 1.3.",
        "observations": [{"text": "Creatinine 1.3 mg/dL, above 0.6 - 1.2.", "category": "value", "sources": [], "confidence": 0.9},
                         {"text": "Potassium 4.5 mmol/L within 3.5 - 5.0.", "category": "value", "sources": [], "confidence": 0.9}],
        "missing_records": [], "cross_domain_flags": [], "declined_advice_request": False, "decline_note": None, "confidence": 0.9})
    dropped = board.ground_findings(f, known)
    assert [o.text for o in f.observations] == ["Creatinine 1.3 mg/dL, above 0.6 - 1.2."]
    assert f.summary == "Creatinine 1.3 mg/dL, above 0.6 - 1.2." and len(dropped) == 2
    co = CoordinatorOutput.model_validate(dict(COORD, headline="Potassium is 4.5.", what_changed=["Potassium 4.5, was 4.6."],
                                                key_observations=[{"text": "Creatinine 1.3 mg/dL.", "category": "value", "sources": [], "confidence": 0.8}]))
    board.ground_coordinator(co, known)
    assert co.what_changed == [] and co.headline == "Creatinine 1.3 mg/dL."


@pytestmark_db
def test_record_question_never_carries_the_advice_banner(accounts):
    """A specialist's own caution must not turn a record question into a refusal."""
    import database as db
    pid = db.create_patient(accounts["editor"], "Farooq Ahmad")
    cautious = lambda parts: dict(_findings_response(parts), declined_advice_request=True, decline_note="I cannot advise.")
    coord = dict(COORD, declined_advice_request=True, decline_note="Please consult the treating doctor.")
    fake = _fake({SpecialistFindings: cautious, CoordinatorOutput: coord})
    run = board.run_board(pid, accounts["viewer"], "How has his kidney function changed?", fake)
    assert run["advice_flagged"] is False
    co = run["coordinator_output"]
    assert co["declined_advice_request"] is False and co["decline_note"] is None
    assert board.REDIRECT not in board.format_markdown(run)
    coord_call = next(c for c in fake.calls if c["purpose"] == "coordinator")
    assert "Refer to the patient as Farooq" in coord_call["system"]
    triage_call = next(c for c in fake.calls if c["purpose"] == "triage")
    assert "Never pass on a request for a recommendation" in triage_call["system"]
