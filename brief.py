"""Pre-visit brief (item 13): one page per appointment with recent changes,
current medications, latest values, pending reviews, and the open questions
for that doctor. Markdown for the screen, PDF for printing or sending.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import database as db

WINDOW_DAYS = 90


def _d(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:16]


def _stems(text: str) -> set[str]:
    import re
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        w = re.sub(r"(ologists?|ology|ists?|ians?|ics?|y)$", "", w)
        if len(w) >= 4:
            out.add(w)
    return out


def _doctor_matches(doc: str, specialty: str, doctor_name: str) -> bool:
    """Does a question group's doctor label fit this appointment? No appointment filter means yes."""
    if not (specialty or doctor_name):
        return True
    ds = _stems(doc)
    for s in _stems(specialty):
        if any(d.startswith(s) or s.startswith(d) for d in ds):
            return True
    if doctor_name:
        words = [w for w in doctor_name.replace(".", " ").split() if len(w) > 2 and w.lower() not in ("dr", "doctor", "prof")]
        if any(w.lower() in doc for w in words):
            return True
    return False


def build_brief(patient_id: int, user: dict, appointment_id: int | None = None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    db.require_role(user, "viewer")
    import appointments as appts
    import board
    import medications as meds

    patient = db.get_patient(patient_id, user)
    if not patient:
        raise PermissionError(f"no patient {patient_id}")
    since = datetime.now() - timedelta(days=days)
    appointment = appts.get_appointment(appointment_id, patient_id, user) if appointment_id else None
    specialty = ((appointment or {}).get("specialty") or "").lower()
    doctor_name = ((appointment or {}).get("doctor_name") or "").lower()

    active = meds.list_medications(patient_id, user, status="active")
    changes = [c for c in meds.medication_history(patient_id, user) if c["changed_at"] and c["changed_at"] >= since]

    latest = db.get_latest_vitals(patient_id, user)
    recent_values = []
    for v in latest:
        when = v["recorded_at"] or v["timestamp"]
        recent_values.append({
            "name": v["reading_type"].replace("lab_", "").replace("_", " "), "value": v["value"], "unit": v["unit"] or "",
            "date": _d(when)[:10], "change": v.get("change_from_previous"), "flag": v.get("flag"),
            "reference_range": v.get("reference_range"), "recent": bool(when and when >= since),
        })

    docs = [d for d in db.list_documents(patient_id, user) if d["uploaded_at"] and d["uploaded_at"] >= since]
    pending = sum(len(v) for v in __import__("documents").pending_review(patient_id, user).values())

    questions: list[dict[str, Any]] = []
    missing: list[str] = []
    seen = set()
    for run in board.list_runs(patient_id, user, limit=10):
        co = run.get("coordinator_output") or {}
        if isinstance(co, str):
            co = json.loads(co)
        for group in co.get("questions_by_doctor", []):
            doc = (group.get("doctor") or "").lower()
            relevant = _doctor_matches(doc, specialty, doctor_name)
            for q in group.get("questions", []):
                key = q.strip().lower()
                if key in seen or not relevant:
                    continue
                seen.add(key)
                questions.append({"doctor": group.get("doctor"), "question": q, "run_id": run["id"], "date": _d(run["created_at"])[:10]})
        for m in co.get("missing_records", []):
            if m not in missing:
                missing.append(m)

    memories = [m for m in db.get_doctor_memories(patient_id, user, limit=40) if m["doctor_type"] != db.SHARED_AGENT][:8]
    import memory as memory_mod
    profile_facts = memory_mod.profile(patient_id, user)
    import consultations
    conversations = consultations.recent_for_brief(patient_id, user, since)
    return {
        "patient": {k: patient.get(k) for k in ("id", "name", "age", "date_of_birth", "sex", "allergies", "primary_doctor", "medical_history")},
        "appointment": appointment,
        "generated_at": datetime.now(),
        "window_days": days,
        "medications": active,
        "medication_changes": changes,
        "values": recent_values,
        "documents": docs,
        "pending_review": pending,
        "questions": questions[:25],
        "missing_records": missing[:10],
        "recent_notes": [{"doctor": m["doctor_type"], "text": m["content"][:300], "date": _d(m["created_at"])[:10]} for m in memories],
        "conversations": conversations,
        "profile": [{"text": f["content"], "category": f.get("category") or "other"} for f in profile_facts],
    }


_LABELS = {
    "en": {"title": "Pre-visit brief", "patient": "Patient", "appointment": "Appointment", "generated": "Prepared",
           "meds": "Current medications", "changes": "Medication changes in the last {d} days", "values": "Latest values",
           "docs": "Documents added in the last {d} days", "questions": "Questions for this visit", "missing": "Records we are missing",
           "pending": "{n} extracted items still await family review", "notes": "Notes from earlier reviews",
           "conversations": "Doctor conversations in the last {d} days", "actions": "Asked of us",
           "profile": "Established facts and standing instructions",
           "none": "none", "disclaimer": "This brief summarizes the family's records. It is not medical advice."},
    "ur": {"title": "ملاقات سے پہلے کا خلاصہ", "patient": "مریض", "appointment": "ملاقات", "generated": "تیار کیا گیا",
           "meds": "موجودہ ادویات", "changes": "پچھلے {d} دنوں میں ادویات کی تبدیلیاں", "values": "تازہ ترین نتائج",
           "docs": "پچھلے {d} دنوں میں شامل دستاویزات", "questions": "اس ملاقات کے لیے سوالات", "missing": "جو ریکارڈ ہمارے پاس نہیں",
           "pending": "{n} نکالے گئے آئٹمز ابھی خاندان کے جائزے کے منتظر ہیں", "notes": "پچھلے جائزوں کے نوٹس",
           "conversations": "پچھلے {d} دنوں میں ڈاکٹر سے گفتگو", "actions": "ہم سے کہا گیا",
           "profile": "طے شدہ حقائق اور ڈاکٹروں کی مستقل ہدایات",
           "none": "کوئی نہیں", "disclaimer": "یہ خلاصہ خاندان کے ریکارڈ کا ہے۔ یہ طبی مشورہ نہیں ہے۔"},
}


def brief_markdown(brief: dict[str, Any], language: str = "en") -> str:
    L = _LABELS.get(language, _LABELS["en"])
    d = brief["window_days"]
    p = brief["patient"]
    a = brief.get("appointment")
    out = [f"# {L['title']}: {p['name']}"]
    out.append(f"**{L['patient']}:** {p['name']}" + (f", {p['age']}" if p.get("age") else "") + (f", {p['sex']}" if p.get("sex") else ""))
    if p.get("allergies"):
        out.append(f"**Allergies:** {p['allergies']}")
    if a:
        out.append(f"**{L['appointment']}:** {_d(a['starts_at'])} {a['title']}"
                   + (f" with {a['doctor_name']}" if a.get("doctor_name") else "") + (f" ({a['specialty']})" if a.get("specialty") else "")
                   + (f", {a['location']}" if a.get("location") else ""))
    out.append(f"**{L['generated']}:** {_d(brief['generated_at'])}")
    out.append(f"\n_{L['disclaimer']}_\n")

    out.append(f"## {L['meds']}")
    if brief["medications"]:
        for m in brief["medications"]:
            out.append(f"- {m['name']} {m.get('dose') or ''} {m.get('frequency') or ''}".rstrip()
                       + (f" ({m['prescriber']})" if m.get("prescriber") else "") + (f", since {_d(m['start_date'])}" if m.get("start_date") else ""))
    else:
        out.append(f"- {L['none']}")

    out.append(f"\n## {L['changes'].format(d=d)}")
    if brief["medication_changes"]:
        for c in brief["medication_changes"]:
            out.append(f"- {_d(c['changed_at'])[:10]}: {c['change_type']} {c.get('medication_name') or c.get('name') or ''}")
    else:
        out.append(f"- {L['none']}")

    out.append(f"\n## {L['values']}")
    if brief["values"]:
        for v in brief["values"]:
            change = f" ({v['change']:+.1f})" if v.get("change") is not None else ""
            flag = f" **{v['flag']}**" if v.get("flag") and v["flag"] not in ("normal", "unknown") else ""
            out.append(f"- {v['name']}: {v['value']} {v['unit']} on {v['date']}{change}{flag}")
    else:
        out.append(f"- {L['none']}")

    out.append(f"\n## {L['questions']}")
    if brief["questions"]:
        for q in brief["questions"]:
            out.append(f"- {q['question']}" + (f" _(for {q['doctor']})_" if q.get("doctor") else ""))
    else:
        out.append(f"- {L['none']}")

    if brief["missing_records"]:
        out.append(f"\n## {L['missing']}")
        out += [f"- {m}" for m in brief["missing_records"]]

    out.append(f"\n## {L['docs'].format(d=d)}")
    if brief["documents"]:
        for doc in brief["documents"]:
            out.append(f"- {_d(doc['uploaded_at'])[:10]}: {doc.get('document_kind') or 'document'}, {doc['filename']}")
    else:
        out.append(f"- {L['none']}")
    if brief["pending_review"]:
        out.append(f"\n_{L['pending'].format(n=brief['pending_review'])}_")
    if brief.get("profile"):
        out.append(f"\n## {L['profile']}")
        out += [f"- {f['text']}" for f in brief["profile"][:20]]
    if brief.get("conversations"):
        out.append(f"\n## {L['conversations'].format(d=d)}")
        for c in brief["conversations"]:
            out.append(f"- **{c['date']}** {c['title']}" + (f" ({c['doctor']})" if c.get("doctor") else "") + f": {c['summary']}")
            for a in c.get("actions") or []:
                out.append(f"    - {L['actions']}: {a}")
    if brief["recent_notes"]:
        out.append(f"\n## {L['notes']}")
        out += [f"- [{n['doctor']}] {n['text']}" for n in brief["recent_notes"]]
    return "\n".join(out)


def brief_pdf(brief: dict[str, Any], language: str = "en") -> bytes:
    """Render the brief as a PDF. Urdu needs a font with Arabic script; without
    one on the host the PDF falls back to English labels and Latin text."""
    from fpdf import FPDF

    md = brief_markdown(brief, language)
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    font = "Helvetica"
    unicode_font = _find_unicode_font()
    if unicode_font:
        pdf.add_font("Body", "", unicode_font)
        font = "Body"
    for line in md.splitlines():
        text = line
        size, style = 11, ""
        if line.startswith("# "):
            text, size = line[2:], 16
        elif line.startswith("## "):
            text, size = line[3:], 13
            pdf.ln(3)
        elif line.startswith("- "):
            text = "  • " + line[2:] if unicode_font else "  - " + line[2:]
        text = text.replace("**", "").replace("_", "")
        if not unicode_font:
            text = text.encode("latin-1", "replace").decode("latin-1")
        pdf.set_font(font, style, size)
        if not text.strip():
            pdf.ln(4)
            continue
        pdf.multi_cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _find_unicode_font() -> str | None:
    import glob
    import os

    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ] + glob.glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True) + glob.glob("fonts/*.ttf")
    for c in candidates:
        if os.path.exists(c):
            return c
    return None
