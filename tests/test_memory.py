"""Persistent memory: profile, per-agent notes, lessons from corrections,
consolidation after a run, standing instructions from conversations."""
import datetime as dt
import os

import pytest

import board
import memory
from providers import FakeProvider
from tests.test_board import RESPONSES

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def test_score_prefers_relevance_then_decayed_importance():
    now = dt.datetime(2026, 10, 15)
    fresh = {"content": "Blood pressure 150 over 90 in September", "importance": 5, "created_at": now - dt.timedelta(days=10)}
    old = {"content": "Blood pressure 160 over 95 last year", "importance": 9, "created_at": now - dt.timedelta(days=400)}
    other = {"content": "HbA1c 7.8 percent", "importance": 9, "created_at": now}
    q = memory._words("what do the records show about blood pressure")
    assert memory.score(fresh, q, now) > memory.score(other, q, now)  # relevance beats importance
    assert memory.score(old, q, now) < memory.score(fresh, q, now) + 10  # decay: nine points minus about seven months
    confirmed = dict(old, last_confirmed_at=now)
    assert memory.score(confirmed, q, now) > memory.score(old, q, now)


def test_each_specialist_sees_its_own_notes_profile_and_lessons(accounts, migrated):
    admin, editor = accounts["admin"], accounts["editor"]
    pid = migrated.create_patient(admin, "Parent One")
    migrated.save_doctor_memory(pid, editor, "cardiologist", "review", "Pressure trend reviewed in September", importance=5)
    migrated.save_doctor_memory(pid, editor, "nephrologist", "review", "Creatinine stable at 1.3", importance=5)
    migrated.save_doctor_memory(pid, editor, migrated.SHARED_AGENT, "fact", "Allergic to penicillin", importance=9, category="allergy")
    cardio = memory.notes_block(pid, editor, "cardiologist", "blood pressure")
    assert "Pressure trend" in cardio and "Creatinine" not in cardio
    assert memory.notes_block(pid, editor, "pharmacist") == ""
    shared = memory.context_block(pid, editor)
    assert "PATIENT PROFILE" in shared and "penicillin" in shared and "Pressure trend" not in shared
    # A correction becomes a lesson every specialist sees, for every patient.
    note = next(m for m in migrated.list_doctor_memories(pid, editor) if m["doctor_type"] == "cardiologist")
    migrated.update_doctor_memory(note["id"], pid, editor, "Pressure trend reviewed in August, not September")
    other = migrated.create_patient(admin, "Parent Two")
    block = memory.context_block(other, editor)
    assert "LESSONS" in block and "corrected a note from cardiologist" in block and "August" in block
    assert len(migrated.get_lessons(editor)) == 1
    # Removing a note is a lesson too; deleting a lesson is not.
    migrated.delete_doctor_memory(note["id"], pid, editor)
    lessons = migrated.get_lessons(editor)
    assert len(lessons) == 2 and "removed a note" in lessons[0]["content"]
    migrated.delete_lesson(lessons[0]["id"], editor)
    assert len(migrated.get_lessons(editor)) == 1
    # The board context carries profile and lessons, the specialist gets its notes.
    ctx = board.build_context(pid, editor)
    assert "penicillin" in ctx and "LESSONS" in ctx


def test_consolidation_updates_the_profile_and_keeps_history(accounts, migrated):
    admin, editor = accounts["admin"], accounts["editor"]
    pid = migrated.create_patient(admin, "Parent One")
    old_id = migrated.save_doctor_memory(pid, editor, migrated.SHARED_AGENT, "fact", "HbA1c 8.4 percent in June 2026", importance=6, category="baseline")
    update = {"new_facts": [
        {"text": "HbA1c 7.8 percent in September 2026", "category": "baseline", "importance": 6, "replaces_ids": [old_id]},
        {"text": "Type 2 diabetes, followed by Dr. Khan (endocrinology)", "category": "diagnosis", "importance": 8, "replaces_ids": []},
        {"text": "HbA1c 7.8 percent in September 2026", "category": "baseline", "importance": 6, "replaces_ids": []},  # duplicate, dropped
    ], "contradictions": ["The lab report lists the patient's age as 81, the profile says 79."], "unchanged": False}
    fake = FakeProvider(responses={**RESPONSES, memory.ProfileUpdate: update})
    run = board.run_board(pid, editor, "What do the latest records show about his diabetes?", fake)
    cons = run["consolidation"]
    assert cons["error"] is None and len(cons["added"]) == 2 and cons["superseded"] == 1
    assert cons["contradictions"] == ["The lab report lists the patient's age as 81, the profile says 79."]
    facts = memory.profile(pid, editor)
    texts = [f["content"] for f in facts]
    assert "HbA1c 7.8 percent in September 2026" in texts and "HbA1c 8.4 percent in June 2026" not in texts
    history = migrated.list_doctor_memories(pid, editor)
    old = next(m for m in history if m["id"] == old_id)
    assert old["status"] == "superseded" and old["superseded_by"] is not None
    assert any(m["memory_type"] == "contradiction" and m["doctor_type"] == "coordinator" for m in history)
    # The consolidation call is cheap and separate from the specialists.
    assert cons["call"]["purpose"] == "memory_consolidate"
    # Restore brings the old fact back.
    migrated.restore_doctor_memory(old_id, pid, editor)
    assert "HbA1c 8.4 percent in June 2026" in [f["content"] for f in memory.profile(pid, editor)]
    # Confirming a fact stamps it.
    migrated.confirm_doctor_memory(old_id, pid, editor)
    assert next(m for m in migrated.list_doctor_memories(pid, editor) if m["id"] == old_id)["last_confirmed_at"] is not None


def test_consolidation_failure_does_not_lose_the_run(accounts, migrated):
    admin, editor = accounts["admin"], accounts["editor"]
    pid = migrated.create_patient(admin, "Parent One")
    fake = FakeProvider(responses=dict(RESPONSES))  # no ProfileUpdate
    run = board.run_board(pid, editor, "What do the records show about blood pressure?", fake)
    assert run["status"] == "done" and run["consolidation"]["error"]
    assert memory.profile(pid, editor) == []


def test_conversation_writes_standing_instructions_to_the_profile(accounts, migrated):
    admin, editor = accounts["admin"], accounts["editor"]
    pid = migrated.create_patient(admin, "Parent One")
    summary = {"doctor_name": "Dr. Lee", "doctor_specialty": "Cardiology", "summary": "Pressure still high.",
               "actions": [{"text": "Check pressure every morning", "who": "family", "when": None, "kind": "monitoring", "confidence": 0.9},
                           {"text": "Come back in four weeks", "who": "family", "when": "in four weeks", "kind": "appointment", "confidence": 0.9},
                           {"text": "Maybe reduce salt", "who": "patient", "when": None, "kind": "lifestyle", "confidence": 0.3}]}
    ids = memory.record_consultation(pid, editor, summary, consultation_id=None, file_id=None, happened_on=dt.date(2026, 10, 10))
    facts = memory.profile(pid, editor)
    texts = [f["content"] for f in facts]
    assert any("Dr. Lee (Cardiology) saw the patient on 2026-10-10" in t for t in texts)
    assert any("Dr. Lee asked on 2026-10-10: Check pressure every morning" in t for t in texts)
    assert not any("salt" in t for t in texts) and not any("Come back" in t for t in texts)
    assert [f["memory_type"] for f in facts].count("standing_instruction") == 1
    notes = migrated.get_doctor_memories(pid, editor, doctor_type="general_duty")
    assert len(notes) == 1 and "Pressure still high" in notes[0]["content"]
    assert len(ids) == 3
