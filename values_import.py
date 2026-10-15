"""Bulk import of readings from a spreadsheet (CSV or Excel).

parse_table() turns the file into preview rows the caregiver can correct;
add_readings() writes the rows they approved. Column names are matched
loosely (date, type, value, unit, systolic, diastolic, pulse, notes) and a
wide layout with one column per reading type is handled too.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, time as dtime
from typing import Any

import pandas as pd

import database as db
from extraction import parse_date, slug

_ALIASES = {
    "recorded_at": ["date", "datetime", "time", "when", "recorded", "taken", "timestamp", "day"],
    "reading_type": ["type", "reading", "test", "measure", "measurement", "parameter", "name", "vital"],
    "value": ["value", "result", "reading value", "level", "amount"],
    "unit": ["unit", "units"],
    "systolic": ["systolic", "sys", "bp systolic", "sbp", "upper"],
    "diastolic": ["diastolic", "dia", "bp diastolic", "dbp", "lower"],
    "pulse": ["pulse", "heart rate", "hr", "bpm"],
    "notes": ["notes", "note", "comment", "comments", "remarks"],
}
_WIDE = {
    "glucose": ["glucose", "sugar", "blood sugar", "bsl", "fasting", "random sugar", "cbg"],
    "weight": ["weight", "wt", "kg"],
    "oxygen_saturation": ["spo2", "oxygen", "saturation", "o2"],
    "temperature": ["temperature", "temp", "fever"],
    "heart_rate": ["heart rate", "pulse", "hr"],
}
_TYPE_WORDS = {
    "blood_pressure_systolic": ["systolic"], "blood_pressure_diastolic": ["diastolic"],
    "glucose": ["glucose", "sugar", "bsl", "cbg"], "weight": ["weight"], "oxygen_saturation": ["spo2", "oxygen", "saturation"],
    "temperature": ["temp"], "heart_rate": ["pulse", "heart"],
}


def _norm(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name).strip().lower()).strip()


def _find(columns: list[str], aliases: list[str]) -> str | None:
    normed = {c: _norm(c) for c in columns}
    for alias in aliases:
        for c, n in normed.items():
            if n == alias:
                return c
    for alias in aliases:
        for c, n in normed.items():
            if alias in n:
                return c
    return None


def _when(v: Any) -> datetime | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime().replace(tzinfo=None)
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return datetime.combine(v, dtime(0, 0))
    text = str(v).strip()
    if not text:
        return None
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}", text):  # ISO: year first, never day first
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            pass
    try:
        return pd.to_datetime(text, dayfirst=True).to_pydatetime().replace(tzinfo=None)
    except Exception:
        d = parse_date(text)
        return datetime.combine(d, dtime(0, 0)) if d else None


def _num(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def _reading_type(label: str) -> str:
    n = _norm(label)
    for rt, words in _TYPE_WORDS.items():
        if any(w in n for w in words):
            return rt
    if "bp" in n.split() or "blood pressure" in n:
        return "blood_pressure"
    return slug(label) or "other"


def read_frame(data: bytes, filename: str) -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(io.BytesIO(data))
    text = data.decode("utf-8-sig", errors="replace")
    sep = "\t" if text.count("\t") > text.count(",") else ","
    return pd.read_csv(io.StringIO(text), sep=sep)


def parse_table(data: bytes, filename: str) -> list[dict[str, Any]]:
    """Rows: {reading_type, value, unit, recorded_at, notes, source_row}. Unparseable cells are skipped."""
    df = read_frame(data, filename)
    df = df.dropna(how="all")
    cols = [str(c) for c in df.columns]
    df.columns = cols
    c_when = _find(cols, _ALIASES["recorded_at"])
    c_type = _find(cols, _ALIASES["reading_type"])
    c_value = _find(cols, _ALIASES["value"])
    c_unit = _find(cols, _ALIASES["unit"])
    c_sys = _find(cols, _ALIASES["systolic"])
    c_dia = _find(cols, _ALIASES["diastolic"])
    c_pulse = _find(cols, _ALIASES["pulse"])
    c_notes = _find(cols, _ALIASES["notes"])
    used = {c for c in (c_when, c_type, c_value, c_unit, c_sys, c_dia, c_pulse, c_notes) if c}
    wide_cols = {}
    for c in cols:
        if c in used:
            continue
        for rt, words in _WIDE.items():
            if any(w in _norm(c) for w in words):
                wide_cols[c] = rt
                break
    rows: list[dict[str, Any]] = []
    for i, rec in df.iterrows():
        when = _when(rec[c_when]) if c_when else None
        notes = str(rec[c_notes]).strip() if c_notes and not pd.isna(rec[c_notes]) else ""
        if c_sys and c_dia:
            s, d = _num(rec[c_sys]), _num(rec[c_dia])
            if s is not None and d is not None:
                rows.append({"reading_type": "blood_pressure_systolic", "value": s, "unit": "mmHg", "recorded_at": when, "notes": notes, "source_row": int(i) + 2})
                rows.append({"reading_type": "blood_pressure_diastolic", "value": d, "unit": "mmHg", "recorded_at": when, "notes": notes, "source_row": int(i) + 2})
        if c_pulse:
            pval = _num(rec[c_pulse])
            if pval is not None:
                rows.append({"reading_type": "heart_rate", "value": pval, "unit": "bpm", "recorded_at": when, "notes": notes, "source_row": int(i) + 2})
        if c_type and c_value:
            label = str(rec[c_type]) if not pd.isna(rec[c_type]) else ""
            raw = rec[c_value]
            rt = _reading_type(label) if label else "other"
            unit = str(rec[c_unit]).strip() if c_unit and not pd.isna(rec[c_unit]) else None
            if rt == "blood_pressure":
                m = re.match(r"\s*(\d+)\s*/\s*(\d+)", str(raw))
                if m:
                    rows.append({"reading_type": "blood_pressure_systolic", "value": float(m.group(1)), "unit": "mmHg", "recorded_at": when, "notes": notes, "source_row": int(i) + 2})
                    rows.append({"reading_type": "blood_pressure_diastolic", "value": float(m.group(2)), "unit": "mmHg", "recorded_at": when, "notes": notes, "source_row": int(i) + 2})
            else:
                v = _num(raw)
                if v is not None and label:
                    rows.append({"reading_type": rt, "value": v, "unit": unit, "recorded_at": when, "notes": notes, "source_row": int(i) + 2})
        for c, rt in wide_cols.items():
            v = _num(rec[c])
            if v is not None:
                rows.append({"reading_type": rt, "value": v, "unit": None, "recorded_at": when, "notes": notes, "source_row": int(i) + 2})
    return rows


def add_readings(patient_id: int, user: dict, rows: list[dict[str, Any]], source: str) -> int:
    """Write approved rows. Returns the number written."""
    n = 0
    for r in rows:
        if r.get("value") is None or not r.get("reading_type"):
            continue
        db.save_vital_reading(patient_id, user, str(r["reading_type"]), float(r["value"]), r.get("unit") or None,
                              source=source, notes=r.get("notes") or "", recorded_at=r.get("recorded_at"))
        n += 1
    return n
