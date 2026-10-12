"""Unit tests for utils.py. No database or Streamlit needed."""
from datetime import datetime

from utils import (
    dev_user_id,
    normalize_extracted_readings,
    parse_addressed_doctor,
    parse_reading_timestamp,
)


# parse_addressed_doctor

def test_addressed_doctor_matches_role_word():
    assert parse_addressed_doctor("Please ask the cardiologist about this ECG") == "cardiologist"


def test_addressed_doctor_respects_word_boundary():
    # "endorse" contains "endo" but is not the endocrinologist.
    assert parse_addressed_doctor("I endorse this plan") is None


def test_addressed_doctor_filter_excludes_inactive_agent():
    assert parse_addressed_doctor("ask the cardiologist", agent_types=["neurologist"]) is None
    assert parse_addressed_doctor("ask the cardiologist", agent_types=["cardiologist"]) == "cardiologist"


def test_addressed_doctor_empty_message():
    assert parse_addressed_doctor("") is None
    assert parse_addressed_doctor(None) is None


# parse_reading_timestamp

def test_timestamp_iso_with_z():
    assert parse_reading_timestamp("2026-03-04T08:15:00Z") == datetime(2026, 3, 4, 8, 15, 0)


def test_timestamp_space_separated():
    assert parse_reading_timestamp("2026-03-04 08:15") == datetime(2026, 3, 4, 8, 15)


def test_timestamp_day_first_slash():
    assert parse_reading_timestamp("04/03/2026") == datetime(2026, 3, 4)


def test_timestamp_garbage_is_none():
    assert parse_reading_timestamp("sometime last week") is None


def test_timestamp_none_and_empty_are_none():
    assert parse_reading_timestamp(None) is None
    assert parse_reading_timestamp("") is None
    assert parse_reading_timestamp("   ") is None


# normalize_extracted_readings

def test_cgm_rows_carry_recorded_at_when_present():
    data = [
        {"timestamp": "2026-03-04T08:15:00Z", "glucose_mg_dl": 142, "notes": "fasting"},
        {"glucose_mg_dl": 160},
    ]
    rows = normalize_extracted_readings("cgm", data)
    assert len(rows) == 2
    assert rows[0]["reading_type"] == "glucose"
    assert rows[0]["value"] == 142.0
    assert rows[0]["unit"] == "mg/dL"
    assert rows[0]["recorded_at"] == datetime(2026, 3, 4, 8, 15)
    assert rows[0]["notes"] == "fasting"
    assert rows[1]["recorded_at"] is None


def test_blood_pressure_three_rows_with_pulse_two_without():
    with_pulse = [{"timestamp": "2026-03-04 08:15", "systolic": 135, "diastolic": 82, "pulse": 72}]
    without_pulse = [{"systolic": 128, "diastolic": 79}]
    rows = normalize_extracted_readings("blood_pressure", with_pulse)
    assert [r["reading_type"] for r in rows] == [
        "blood_pressure_systolic", "blood_pressure_diastolic", "heart_rate",
    ]
    assert [r["value"] for r in rows] == [135.0, 82.0, 72.0]
    assert all(r["recorded_at"] == datetime(2026, 3, 4, 8, 15) for r in rows)

    rows = normalize_extracted_readings("blood_pressure", without_pulse)
    assert [r["reading_type"] for r in rows] == ["blood_pressure_systolic", "blood_pressure_diastolic"]


def test_lab_results_slug_and_reference_note():
    data = [{"test_name": "HbA1c", "value": "7.2", "unit": "%", "reference_range": "4.0-5.6"}]
    rows = normalize_extracted_readings("lab_results", data)
    assert len(rows) == 1
    assert rows[0]["reading_type"] == "lab_hba1c"
    assert rows[0]["value"] == 7.2
    assert rows[0]["unit"] == "%"
    assert "Reference:" in rows[0]["notes"]
    assert "4.0-5.6" in rows[0]["notes"]


def test_non_list_input_yields_nothing():
    assert normalize_extracted_readings("cgm", {"analysis": "text"}) == []
    assert normalize_extracted_readings("lab_results", {"raw_response": "..."}) == []
    assert normalize_extracted_readings("cgm", None) == []
    assert normalize_extracted_readings("cgm", "not json") == []


def test_non_numeric_values_are_skipped():
    cgm = [{"glucose_mg_dl": "n/a"}, {"glucose_mg_dl": 110}]
    assert [r["value"] for r in normalize_extracted_readings("cgm", cgm)] == [110.0]

    labs = [{"test_name": "Culture", "value": "no growth"}, {"test_name": "Sodium", "value": 138}]
    rows = normalize_extracted_readings("lab_results", labs)
    assert [r["reading_type"] for r in rows] == ["lab_sodium"]

    bp = [{"systolic": "high", "diastolic": 80}]
    assert normalize_extracted_readings("blood_pressure", bp) == []


def test_general_type_routes_by_reading_type_field():
    data = [
        {"type": "glucose", "value": 120, "timestamp": "2026-03-04"},
        {"type": "blood_pressure", "systolic": 130, "diastolic": 85},
        {"type": "lab_test", "test_name": "Creatinine", "value": 1.1, "unit": "mg/dL"},
        {"type": "note", "value": "free text"},
    ]
    rows = normalize_extracted_readings("general", data)
    assert [r["reading_type"] for r in rows] == [
        "glucose", "blood_pressure_systolic", "blood_pressure_diastolic", "lab_creatinine",
    ]


def test_rows_keep_raw_reading():
    data = [{"glucose_mg_dl": 99, "extra": "kept"}]
    rows = normalize_extracted_readings("cgm", data)
    assert rows[0]["raw"] is data[0]


# dev_user_id

def test_dev_user_id_is_deterministic():
    assert dev_user_id("Alice") == dev_user_id("Alice")


def test_dev_user_id_ignores_case_and_whitespace():
    assert dev_user_id("Alice") == dev_user_id("  alice ")


def test_dev_user_id_differs_by_name():
    assert dev_user_id("Alice") != dev_user_id("Bob")


def test_dev_user_id_prefix():
    assert dev_user_id("Alice").startswith("dev-")
