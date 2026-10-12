"""Pure helpers with no Streamlit or database dependencies.

Kept separate so they can be unit tested without a running app or Postgres.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable

# Keyword patterns used to route a chat message to one specialist.
# Word boundaries prevent "endo" matching "endorse" and similar.
DOCTOR_PATTERNS: dict[str, list[str]] = {
    "neurologist": ["neurologist", "neuro", "stroke doctor"],
    "cardiologist": ["cardiologist", "cardio", "heart doctor"],
    "nephrologist": ["nephrologist", "nephro", "kidney doctor"],
    "endocrinologist": ["endocrinologist", "endo", "diabetes doctor"],
    "radiologist": ["radiologist", "radiology", "imaging"],
}


def parse_addressed_doctor(message: str, agent_types: Iterable[str] | None = None) -> str | None:
    """Return the agent_type a message is addressed to, or None.

    If agent_types is given, only those agents are considered.
    """
    text = (message or "").lower()
    allowed = set(agent_types) if agent_types is not None else None
    for doctor, patterns in DOCTOR_PATTERNS.items():
        if allowed is not None and doctor not in allowed:
            continue
        for pattern in patterns:
            if re.search(r"\b" + re.escape(pattern) + r"\b", text):
                return doctor
    return None


_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


def parse_reading_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp string returned by the extraction model.

    Returns None when the value is missing or unparseable. Callers must treat
    None as "time of reading unknown" rather than substituting now().
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def normalize_extracted_readings(file_type: str, data: Any) -> list[dict[str, Any]]:
    """Turn the model's JSON into rows ready for vitals_history.

    Each row has: reading_type, value, unit, recorded_at (datetime or None),
    notes, raw (dict). Rows with no numeric value are dropped. Free-text
    outputs (x-ray analysis, raw_response) produce no rows.
    """
    if not isinstance(data, list):
        return []
    rows: list[dict[str, Any]] = []
    for reading in data:
        if not isinstance(reading, dict):
            continue
        kind = (reading.get("type") or "").lower()
        ts = parse_reading_timestamp(reading.get("timestamp"))

        if file_type == "cgm" or kind == "glucose":
            value = _num(reading.get("glucose_mg_dl") if reading.get("glucose_mg_dl") is not None else reading.get("value"))
            if value is None:
                continue
            rows.append({
                "reading_type": "glucose",
                "value": value,
                "unit": reading.get("unit") or "mg/dL",
                "recorded_at": ts,
                "notes": reading.get("notes") or "",
                "raw": reading,
            })

        elif file_type == "blood_pressure" or kind == "blood_pressure":
            systolic = _num(reading.get("systolic"))
            diastolic = _num(reading.get("diastolic"))
            pulse = _num(reading.get("pulse"))
            if systolic is not None and diastolic is not None:
                rows.append({"reading_type": "blood_pressure_systolic", "value": systolic, "unit": "mmHg",
                             "recorded_at": ts, "notes": reading.get("notes") or "", "raw": reading})
                rows.append({"reading_type": "blood_pressure_diastolic", "value": diastolic, "unit": "mmHg",
                             "recorded_at": ts, "notes": reading.get("notes") or "", "raw": reading})
            if pulse is not None:
                rows.append({"reading_type": "heart_rate", "value": pulse, "unit": "bpm",
                             "recorded_at": ts, "notes": reading.get("notes") or "", "raw": reading})

        elif file_type == "lab_results" or kind == "lab_test":
            name = reading.get("test_name") or reading.get("name")
            value = _num(reading.get("value"))
            if not name or value is None:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
            ref = reading.get("reference_range") or ""
            rows.append({
                "reading_type": f"lab_{slug}",
                "value": value,
                "unit": reading.get("unit") or "",
                "recorded_at": ts,
                "notes": f"Reference: {ref}" if ref else "",
                "raw": reading,
            })
    return rows


def dev_user_id(name: str) -> str:
    """Deterministic user id for the development login.

    The same name always maps to the same id, so a developer can sign out and
    back in and still see their patients. Not for production use.
    """
    digest = hashlib.sha256(name.strip().lower().encode("utf-8")).hexdigest()
    return f"dev-{digest[:16]}"


def json_dumps_safe(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str)
