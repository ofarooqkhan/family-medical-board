"""Board tests: scope enforcement prompt set (no database) and a full run on the fake provider (database)."""
import os

import pytest

import board
from board import CoordinatorOutput, RoutingDecision, SpecialistOutput, detect_advice_request, scrub_advice
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


def _specialist_response(parts):
    text = parts[0].text
    agent = "cardiologist"
    return {
        "agent_type": agent, "summary": "BP readings rose over the summer. You should increase the dose.",
        "observations": [{"text": "Systolic 150 on 2026-09-01 vs 132 in May.", "category": "trend",
                          "sources": [{"file_id": 3, "page": 1, "label": "BP log p1"}], "confidence": 0.8}],
        "questions_for_doctor": [{"question": "Is the rise in systolic pressure expected?", "for_doctor": "cardiologist",
                                  "why": "trend", "priority": "medium"}],
        "missing_records": ["No ECG on file"], "cross_domain_flags": [{"item": "amlodipine", "also_concerns": ["nephrologist"], "why": "swelling"}],
        "declined_advice_request": "ADVICE" in text, "decline_note": None, "confidence": 0.8,
    }


COORD = {
    "headline": "Blood pressure has risen since May. We recommend stopping the salt.",
    "what_changed": ["Systolic pressure up by 18 points since May."],
    "key_observations": [{"text": "Systolic 150 on 2026-09-01.", "category": "value",
                          "sources": [{"file_id": 3, "page": 1, "label": "BP log p1"}], "confidence": 0.8}],
    "questions_by_doctor": [{"doctor": "Cardiologist", "questions": ["Is the rise in systolic pressure expected?"]}],
    "missing_records": ["No ECG on file"], "disagreements": [], "declined_advice_request": False, "decline_note": None,
}

pytestmark_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@pytestmark_db
def test_run_board_logs_cost_and_scrubs_advice(accounts):
    import database as db
    user = accounts["editor"]
    pid = db.create_patient(user, "Parent One", age=81)
    db.save_vital_reading(pid, user, "blood_pressure_systolic", 150, "mmHg")
    fake = FakeProvider(responses={SpecialistOutput: _specialist_response, CoordinatorOutput: COORD})
    run = board.run_board(pid, user, "How has his blood pressure changed?", fake)
    assert run["routed_agents"] and run["routed_agents"][0] == "cardiologist"
    assert run["status"] == "done" and run["advice_flagged"] is False
    assert run["estimated_cost_usd"] is not None and run["input_tokens"] > 0
    assert [c["purpose"] for c in run["model_calls"]][-1] == "coordinator"
    assert "recommend" not in run["coordinator_output"]["headline"]
    assert "You should" not in run["specialist_outputs"][0]["summary"]
    assert run["coordinator_output"]["key_observations"][0]["sources"][0]["file_id"] == 3
    stored = board.get_run(run["id"], pid, user)
    assert stored["estimated_cost_usd"] == pytest.approx(run["estimated_cost_usd"])
    assert stored["routed_agents"]["reason"] == "keyword match"
    assert any(r["action"] == "board.run" for r in db.get_audit_log(pid))
    assert board.get_run(run["id"], pid + 1, user) is None
    md = board.format_markdown(run)
    assert "Questions to ask" in md and "Cardiologist" in md


@pytestmark_db
def test_run_board_declines_advice_and_still_summarizes(accounts):
    import database as db
    user = accounts["viewer"]
    pid = db.create_patient(accounts["editor"], "Parent One")
    fake = FakeProvider(responses={SpecialistOutput: _specialist_response, CoordinatorOutput: COORD})
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
        return _specialist_response(parts)

    fake = FakeProvider(responses={SpecialistOutput: flaky, CoordinatorOutput: COORD})
    run = board.run_board(pid, user, "full review", fake, agents=_agents())
    assert run["status"] == "partial" and run["errors"]
    assert len(run["specialist_outputs"]) == 3


@pytestmark_db
def test_every_specialist_remembers_its_review_and_memories_are_editable(accounts):
    import database as db
    admin, editor, viewer = accounts["admin"], accounts["editor"], accounts["viewer"]
    pid = db.create_patient(admin, "Parent One")
    provider = FakeProvider(responses={SpecialistOutput: _specialist_response, CoordinatorOutput: COORD})
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
