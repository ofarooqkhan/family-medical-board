"""Spreadsheet import parsing (no database) and writing (database)."""
import io
import os
from datetime import datetime

import pytest

import values_import as vi


def test_long_format_csv_with_bp_slash_and_units():
    csv = b"Date,Type,Value,Unit,Notes\n01/10/2026,Blood pressure,142/88,mmHg,morning\n02/10/2026,Fasting sugar,150,mg/dL,\n03/10/2026,Weight,71,kg,after breakfast\n"
    rows = vi.parse_table(csv, "readings.csv")
    types = [r["reading_type"] for r in rows]
    assert types == ["blood_pressure_systolic", "blood_pressure_diastolic", "glucose", "weight"]
    assert rows[0]["value"] == 142 and rows[1]["value"] == 88 and rows[0]["unit"] == "mmHg"
    assert rows[2]["recorded_at"] == datetime(2026, 10, 2)
    assert rows[3]["notes"] == "after breakfast" and rows[3]["source_row"] == 4


def test_bp_columns_and_pulse():
    csv = b"date,systolic,diastolic,pulse\n2026-10-01 08:00,140,90,72\n2026-10-02 08:00,135,85,\n"
    rows = vi.parse_table(csv, "bp.csv")
    assert [(r["reading_type"], r["value"]) for r in rows] == [
        ("blood_pressure_systolic", 140), ("blood_pressure_diastolic", 90), ("heart_rate", 72),
        ("blood_pressure_systolic", 135), ("blood_pressure_diastolic", 85),
    ]
    assert rows[0]["recorded_at"] == datetime(2026, 10, 1, 8, 0)


def test_wide_format_excel():
    import pandas as pd
    df = pd.DataFrame({"Day": ["2026-10-01", "2026-10-02"], "Glucose": [150, None], "Weight (kg)": [71.5, 71.2], "SpO2": [97, 96]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    rows = vi.parse_table(buf.getvalue(), "log.xlsx")
    assert sorted((r["reading_type"], r["value"]) for r in rows) == sorted([
        ("glucose", 150.0), ("weight", 71.5), ("oxygen_saturation", 97.0), ("weight", 71.2), ("oxygen_saturation", 96.0)])
    assert all(r["recorded_at"] is not None for r in rows)


def test_unrecognised_file_yields_nothing():
    assert vi.parse_table(b"foo,bar\n1,2\n", "x.csv") == []


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_add_readings_writes_rows(accounts):
    import database as db
    ed = accounts["editor"]
    pid = db.create_patient(ed, "Parent One")
    rows = vi.parse_table(b"date,systolic,diastolic\n2026-10-01,140,90\n", "bp.csv")
    assert vi.add_readings(pid, ed, rows, source="Import: bp.csv") == 2
    assert {v["reading_type"] for v in db.get_latest_vitals(pid, ed)} == {"blood_pressure_systolic", "blood_pressure_diastolic"}
    with pytest.raises(PermissionError):
        vi.add_readings(pid, accounts["viewer"], rows, source="x")
