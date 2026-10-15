"""Document service: upload, extract, review, confirm (items 3, 4, 5).

ingest_upload() stores the original and its page images, runs extraction,
and queues every extracted item as pending. confirm_item() is the only path
from an extracted item into medications or vitals_history, and it records
the document, page, and item it came from.
"""
from __future__ import annotations

import os
import time
from typing import Any

import database as db
import extraction
from providers import DEFAULT_MODELS, Provider, ProviderError

KIND_LABELS = {
    "prescription": "Prescription",
    "lab_report": "Lab report",
    "imaging_report": "Imaging report",
    "discharge_summary": "Discharge summary or letter",
    "vitals_log": "Readings log (BP, sugar, weight)",
    "consultation_audio": "Doctor conversation (audio)",
    "other": "Other document",
}


# Items the model is sure about go straight into the record; the rest wait
# for a look. The family can turn this off with AUTO_ADD_ITEMS=0.
AUTO_ADD_MIN_CONFIDENCE = 0.6


def auto_add_enabled(override: bool | None = None) -> bool:
    if override is not None:
        return override
    return (os.environ.get("AUTO_ADD_ITEMS") or "1").strip().lower() not in ("0", "false", "no", "off")


def confirmable(item: dict) -> bool:
    """Whether an item carries enough to be written to its home table."""
    p = item.get("payload") or {}
    kind = item.get("item_type")
    if kind == "lab_value":
        return p.get("numeric_value") is not None
    if kind == "vital":
        return bool(extraction.rows_from_vital_payload(p))
    if kind == "medication":
        return bool(p.get("name"))
    if kind == "appointment":
        return bool(p.get("starts_at"))
    return True


def item_label(item: dict) -> str:
    p = item.get("payload") or {}
    kind = item.get("item_type")
    if kind == "medication":
        return " ".join(x for x in (p.get("name"), p.get("dose"), p.get("frequency")) if x)
    if kind == "lab_value":
        v = p.get("numeric_value") if p.get("numeric_value") is not None else p.get("value_text")
        return f"{p.get('test_name') or 'value'} {v if v is not None else ''} {p.get('unit') or ''}".strip()
    if kind == "vital":
        rows = extraction.rows_from_vital_payload(p)
        return ", ".join(f"{r['reading_type'].replace('_', ' ')} {r['value']}" for r in rows) or (p.get("kind") or "reading")
    if kind == "appointment":
        return f"{p.get('title') or 'follow-up'} {p.get('starts_at') or ''}".strip()
    return (p.get("text") or f"{p.get('label', '')}: {p.get('value', '')}").strip()[:120]


def auto_confirm(patient_id: int, file_id: int, user: dict, item_ids: list[int]) -> dict[str, list[str]]:
    """Confirm every new item the model was sure about. Returns what was added
    and what still needs a look, as labels for the summary."""
    added: list[str] = []
    to_check: list[str] = []
    wanted = set(item_ids)
    for it in db.list_extracted_items(patient_id, user, file_id=file_id, status="pending"):
        if it["id"] not in wanted:
            continue
        sure = (it.get("confidence") or 0) >= AUTO_ADD_MIN_CONFIDENCE and not (it["payload"].get("low_confidence_fields") or [])
        if sure and confirmable(it):
            try:
                confirm_item(it["id"], patient_id, user, it["payload"])
                added.append(item_label(it))
                continue
            except Exception:
                pass
        to_check.append(item_label(it))
    return {"added": added, "to_check": to_check}


def ingest_upload(patient_id: int, user: dict, filename: str, mime_type: str, data: bytes,
                  provider: Provider | None, kind: str | None = None, model: str | None = None,
                  auto_add: bool | None = None) -> dict[str, Any]:
    """Store the file, render pages, extract, queue items. Returns a summary dict.

    If provider is None the file is stored without extraction so nothing is lost
    when no model key is configured.
    """
    db.require_role(user, "editor")
    pages = extraction.render_pages(data, mime_type, filename)
    text = "\n\n".join(p["text"] for p in pages if p.get("text")) or None
    file_id = db.save_document(patient_id, user, filename, mime_type, data,
                               document_kind=None if kind in (None, "auto") else kind, pages=pages, extracted_text=text)
    summary: dict[str, Any] = {"file_id": file_id, "pages": len(pages), "items": 0, "kind": kind, "error": None, "calls": []}
    if provider is None:
        return summary
    return run_extraction(patient_id, file_id, user, provider, kind=kind, model=model, pages=pages, summary=summary,
                          auto_add=auto_add)


def run_extraction(patient_id: int, file_id: int, user: dict, provider: Provider, kind: str | None = None,
                   model: str | None = None, pages: list[dict] | None = None, summary: dict | None = None,
                   auto_add: bool | None = None) -> dict[str, Any]:
    """Run (or re-run) extraction for a stored document. Existing pending items are replaced."""
    db.require_role(user, "editor")
    model = model or DEFAULT_MODELS["extraction"]
    summary = summary or {"file_id": file_id, "pages": None, "items": 0, "kind": kind, "error": None, "calls": []}
    if pages is None:
        pages = db.get_document_pages(file_id, patient_id, user)
        summary["pages"] = len(pages)
    t0 = time.time()
    try:
        result = extraction.extract_document(provider, model, pages, kind=kind)
    except ProviderError as e:
        db.set_document_extraction(file_id, patient_id, user, extraction=None, model=model, document_kind=None,
                                   error=str(e))
        summary["error"] = str(e)
        return summary
    doc_date = extraction.parse_date(result.document_date)
    db.set_document_extraction(file_id, patient_id, user, extraction=result.model_dump(), model=model,
                               document_kind=result.kind, document_date=doc_date)
    _discard_pending(patient_id, file_id, user)
    ids = db.add_extracted_items(patient_id, file_id, user, result.items)
    summary.update({"kind": result.kind, "items": len(ids), "item_ids": ids, "calls": result.calls,
                    "duration_ms": int((time.time() - t0) * 1000), "added": [], "to_check": []})
    if auto_add_enabled(auto_add) and ids:
        summary.update(auto_confirm(patient_id, file_id, user, ids))
    else:
        summary["to_check"] = [item_label(i) for i in db.list_extracted_items(patient_id, user, file_id=file_id, status="pending")]
    return summary


def _discard_pending(patient_id: int, file_id: int, user: dict) -> None:
    for it in db.list_extracted_items(patient_id, user, file_id=file_id, status="pending"):
        db.set_extracted_item_status(it["id"], patient_id, user, "discarded")


def pending_review(patient_id: int, user: dict) -> dict[int, list[dict]]:
    """Pending items grouped by file id."""
    grouped: dict[int, list[dict]] = {}
    for it in db.list_extracted_items(patient_id, user, status="pending"):
        grouped.setdefault(it["file_id"], []).append(it)
    return grouped


def confirm_item(item_id: int, patient_id: int, user: dict, payload: dict[str, Any]) -> dict[str, Any]:
    """Caregiver confirmed (possibly corrected) an item. Write it to its home table."""
    db.require_role(user, "editor")
    items = {i["id"]: i for i in db.list_extracted_items(patient_id, user)}
    item = items.get(item_id)
    if item is None:
        raise PermissionError(f"no item {item_id} for patient {patient_id}")
    if item["status"] == "confirmed":
        return {"item_id": item_id, "already": True}
    kind = item["item_type"]
    out: dict[str, Any] = {"item_id": item_id, "item_type": kind}
    if kind == "medication":
        import medications

        med_id = medications.add_medication(
            patient_id, user, name=payload.get("name") or "",
            generic_name=payload.get("generic_name"), dose=payload.get("dose"), route=payload.get("route"),
            frequency=payload.get("frequency"), indication=payload.get("indication"),
            prescriber=payload.get("prescriber"), start_date=extraction.parse_date(payload.get("start_date")),
            notes=payload.get("instructions"), source_file_id=item["file_id"], source_page=item["page_number"],
            extracted_item_id=item_id,
        )
        out["medication_id"] = med_id
    elif kind == "lab_value":
        value = payload.get("numeric_value")
        if value is None:
            raise ValueError("a numeric value is required to confirm a lab value")
        reading_type = payload.get("reading_type") or ("lab_" + extraction.slug(payload.get("test_name") or ""))
        recorded = extraction.parse_date(payload.get("collected_at"))
        from datetime import datetime, time as dtime

        recorded_at = datetime.combine(recorded, dtime(0, 0)) if recorded else None
        vid = db.save_vital_reading(
            patient_id, user, reading_type, float(value), payload.get("unit"),
            source=f"Lab report: {payload.get('lab_name') or 'document ' + str(item['file_id'])}",
            notes=payload.get("test_name") or "", raw_data=payload, recorded_at=recorded_at,
            source_file_id=item["file_id"], source_page=item["page_number"], extracted_item_id=item_id,
            reference_range=payload.get("reference_range"), flag=payload.get("flag"),
        )
        out["vital_id"] = vid
    elif kind == "vital":
        rows = extraction.rows_from_vital_payload(payload)
        if not rows:
            raise ValueError("a numeric value is required to confirm a reading")
        out["vital_ids"] = [
            db.save_vital_reading(patient_id, user, r["reading_type"], r["value"], r["unit"],
                                  source=f"Readings log: document {item['file_id']}", notes=r["notes"], raw_data=payload,
                                  recorded_at=r["recorded_at"], source_file_id=item["file_id"], source_page=item["page_number"],
                                  extracted_item_id=item_id)
            for r in rows
        ]
    elif kind == "appointment":
        import appointments as appts

        starts = appts.to_naive_utc(payload.get("starts_at"))
        if starts is None:
            raise ValueError("a date is required to confirm a follow-up")
        if starts.hour == 0 and starts.minute == 0:
            starts = starts.replace(hour=10)
        out["appointment_id"] = appts.add_appointment(
            patient_id, user, payload.get("title") or "Follow-up", starts, kind=payload.get("kind") or "visit",
            doctor_name=payload.get("doctor_name"), specialty=payload.get("specialty"), notes=payload.get("notes"))
    elif kind in ("imaging_finding", "diagnosis", "fact"):
        text = payload.get("text") or f"{payload.get('label', '')}: {payload.get('value', '')}"
        doctor = {"imaging_finding": "radiologist", "diagnosis": "general_duty", "fact": "general_duty"}[kind]
        db.save_doctor_memory(patient_id, user, doctor, kind, text, importance=6, source_file_id=item["file_id"])
    db.set_extracted_item_status(item_id, patient_id, user, "confirmed", payload=payload)
    _maybe_mark_reviewed(patient_id, item["file_id"], user)
    return out


def reject_item(item_id: int, patient_id: int, user: dict) -> None:
    db.set_extracted_item_status(item_id, patient_id, user, "rejected")
    items = {i["id"]: i for i in db.list_extracted_items(patient_id, user)}
    if item_id in items:
        _maybe_mark_reviewed(patient_id, items[item_id]["file_id"], user)


def _maybe_mark_reviewed(patient_id: int, file_id: int, user: dict) -> None:
    if not db.list_extracted_items(patient_id, user, file_id=file_id, status="pending"):
        db.set_document_status(file_id, patient_id, user, "reviewed")


def stage_text_readings(patient_id: int, user: dict, text: str, provider: Provider, model: str | None = None,
                        auto_add: bool | None = None) -> dict[str, Any]:
    """Typed notes become a stored 'document' (the text itself) with vital items pending review,
    so they go through the same confirm step as a photo."""
    db.require_role(user, "editor")
    model = model or DEFAULT_MODELS["extraction"]
    items, call = extraction.readings_from_text(provider, model, text)
    file_id = db.save_document(patient_id, user, f"typed-readings-{time.strftime('%Y%m%d-%H%M')}.txt", "text/plain",
                               text.encode("utf-8"), document_kind="vitals_log",
                               pages=[{"page_number": 1, "image_png": None, "width": None, "height": None, "text": text}],
                               extracted_text=text)
    db.set_document_extraction(file_id, patient_id, user, extraction={"items": items, "calls": [call.as_dict()]}, model=model,
                               document_kind="vitals_log")
    ids = db.add_extracted_items(patient_id, file_id, user, items)
    out = {"file_id": file_id, "items": len(ids), "item_ids": ids, "calls": [call.as_dict()], "added": [], "to_check": []}
    if auto_add_enabled(auto_add) and ids:
        out.update(auto_confirm(patient_id, file_id, user, ids))
    else:
        out["to_check"] = [item_label(i) for i in items]
    return out
