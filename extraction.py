"""Document extraction into fixed JSON schemas (item 3).

Pipeline: bytes -> pages (PNG render plus any embedded text) -> classification
-> schema-specific extraction with every page image in one call -> a flat list
of items for review. Every item carries the page it came from, an overall
confidence, and the list of fields the model was unsure about, so each field
has a confidence flag. Nothing here writes to the database; documents.py does
that after the caregiver confirms (item 4).
"""
from __future__ import annotations

import io
import re
from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel

from providers import ModelCall, Part, Provider

MAX_PAGES = 12
RENDER_DPI = 150
MAX_IMAGE_SIDE = 2000

DocumentKind = Literal["prescription", "lab_report", "imaging_report", "discharge_summary", "other"]

TRANSCRIBE_RULES = (
    "Transcribe only what is printed or handwritten on the pages. Do not infer, complete, or "
    "normalize beyond what is written; keep drug names, doses, and units as written. If a value is "
    "absent or unreadable, return null. Page numbers are 1-based and refer to the images in order. "
    "confidence is a number from 0 to 1 for the whole item; low_confidence_fields lists the field "
    "names you are not sure about (bad handwriting, smudges, cut-off text). Never add medical advice "
    "or interpretation."
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DocumentClassification(BaseModel):
    kind: DocumentKind
    confidence: float
    patient_name: Optional[str] = None
    document_date: Optional[str] = None  # ISO date as written, if any
    language: Optional[str] = None
    reason: str


class PrescriptionItem(BaseModel):
    name: str
    generic_name: Optional[str] = None
    dose: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    duration: Optional[str] = None
    instructions: Optional[str] = None
    page: int
    confidence: float
    low_confidence_fields: list[str]


class PrescriptionDocument(BaseModel):
    prescriber: Optional[str] = None
    clinic: Optional[str] = None
    patient_name: Optional[str] = None
    document_date: Optional[str] = None
    diagnoses: list[str]
    medications: list[PrescriptionItem]
    notes: Optional[str] = None
    overall_confidence: float


class LabValue(BaseModel):
    test_name: str
    value_text: str
    numeric_value: Optional[float] = None
    unit: Optional[str] = None
    reference_range: Optional[str] = None
    flag: Literal["normal", "high", "low", "abnormal", "unknown"]
    collected_at: Optional[str] = None
    page: int
    confidence: float
    low_confidence_fields: list[str]


class LabReportDocument(BaseModel):
    lab_name: Optional[str] = None
    ordering_doctor: Optional[str] = None
    patient_name: Optional[str] = None
    document_date: Optional[str] = None
    values: list[LabValue]
    comments: Optional[str] = None
    overall_confidence: float


class ImagingFinding(BaseModel):
    text: str
    section: Literal["finding", "impression", "comparison", "technique", "other"]
    page: int
    confidence: float


class ImagingReportDocument(BaseModel):
    modality: Optional[str] = None
    body_part: Optional[str] = None
    study_date: Optional[str] = None
    radiologist: Optional[str] = None
    referring_doctor: Optional[str] = None
    patient_name: Optional[str] = None
    items: list[ImagingFinding]
    overall_confidence: float


class GenericFact(BaseModel):
    label: str
    value: str
    page: int
    confidence: float


class GenericDocument(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    patient_name: Optional[str] = None
    document_date: Optional[str] = None
    summary: str
    facts: list[GenericFact]
    medications_mentioned: list[str]
    overall_confidence: float


SCHEMA_FOR_KIND: dict[str, type[BaseModel]] = {
    "prescription": PrescriptionDocument,
    "lab_report": LabReportDocument,
    "imaging_report": ImagingReportDocument,
    "discharge_summary": GenericDocument,
    "other": GenericDocument,
}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_pages(data: bytes, mime_type: str, filename: str = "") -> list[dict[str, Any]]:
    """Turn a PDF or image into [{page_number, image_png, width, height, text}]."""
    name = (filename or "").lower()
    if mime_type == "application/pdf" or name.endswith(".pdf") or data[:5] == b"%PDF-":
        return _render_pdf(data)
    if name.endswith(".dcm") or mime_type == "application/dicom" or data[128:132] == b"DICM":
        return [_render_dicom(data)]
    return [_render_image(data)]


def _render_dicom(data: bytes) -> dict[str, Any]:
    """DICOM: the pixel data becomes page 1, the header fields become its text."""
    import json

    import numpy as np
    import pydicom
    from PIL import Image
    from pydicom.pixel_data_handlers.util import apply_voi_lut

    ds = pydicom.dcmread(io.BytesIO(data))
    meta = {k: str(getattr(ds, k)) for k in ("PatientName", "StudyDate", "Modality", "BodyPartExamined",
                                               "StudyDescription", "SeriesDescription", "InstitutionName") if hasattr(ds, k)}
    png, w, h = None, None, None
    if hasattr(ds, "pixel_array"):
        arr = apply_voi_lut(ds.pixel_array, ds)
        arr = arr.astype("float64")
        if arr.ndim == 3 and arr.shape[0] > 1 and arr.shape[-1] not in (3, 4):
            arr = arr[arr.shape[0] // 2]  # middle slice of a volume
        lo, hi = float(arr.min()), float(arr.max())
        arr = ((arr - lo) / (hi - lo) * 255).astype(np.uint8) if hi > lo else arr.astype(np.uint8)
        img = Image.fromarray(arr)
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png, w, h = buf.getvalue(), img.width, img.height
    return {"page_number": 1, "image_png": png, "width": w, "height": h, "text": "DICOM header: " + json.dumps(meta)}


def _render_pdf(data: bytes) -> list[dict[str, Any]]:
    import pymupdf

    pages = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc):
            if i >= MAX_PAGES:
                break
            pix = page.get_pixmap(dpi=RENDER_DPI)
            png = pix.tobytes("png")
            png, w, h = _shrink(png)
            pages.append({"page_number": i + 1, "image_png": png, "width": w, "height": h,
                          "text": (page.get_text() or "").strip() or None})
    return pages


def _render_image(data: bytes) -> dict[str, Any]:
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return {"page_number": 1, "image_png": buf.getvalue(), "width": img.width, "height": img.height, "text": None}


def _shrink(png: bytes) -> tuple[bytes, int, int]:
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    if max(img.size) <= MAX_IMAGE_SIDE:
        return png, img.width, img.height
    img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), img.width, img.height


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _page_parts(pages: list[dict[str, Any]]) -> list[Part]:
    parts: list[Part] = []
    for p in pages[:MAX_PAGES]:
        parts.append(Part.of_text(f"Page {p['page_number']} of {min(len(pages), MAX_PAGES)}:"))
        if p.get("image_png"):
            parts.append(Part.of_png(p["image_png"]))
        if p.get("text"):
            parts.append(Part.of_text(f"Embedded text of page {p['page_number']}:\n{p['text'][:6000]}"))
    return parts


def classify(provider: Provider, model: str, pages: list[dict[str, Any]]) -> tuple[DocumentClassification, ModelCall]:
    system = ("You classify one medical document for a family caregiver's record system. "
              "Pick the kind that fits best. " + TRANSCRIBE_RULES)
    first = pages[:2]
    return provider.structured(model=model, system=system, parts=_page_parts(first),
                               schema=DocumentClassification, purpose="classify", max_tokens=400)


def extract(provider: Provider, model: str, pages: list[dict[str, Any]], kind: str) -> tuple[BaseModel, ModelCall]:
    schema = SCHEMA_FOR_KIND.get(kind, GenericDocument)
    focus = {
        "prescription": "This is a prescription or medication list. Capture every medication line as its own item.",
        "lab_report": "This is a laboratory report. Capture every test row as its own item, with the unit and reference range as printed.",
        "imaging_report": "This is an imaging report (X-ray, CT, MRI, ultrasound). Capture each finding and impression sentence as its own item.",
        "discharge_summary": "This is a discharge summary or clinical letter. Capture the key facts and every medication mentioned by name.",
        "other": "Capture the key facts and every medication mentioned by name.",
    }.get(kind, "Capture the key facts.")
    system = f"You extract data from one medical document for a family caregiver's record system. {focus} {TRANSCRIBE_RULES}"
    return provider.structured(model=model, system=system, parts=_page_parts(pages), schema=schema,
                               purpose=f"extract:{kind}", max_tokens=6000)


class ExtractionResult(BaseModel):
    kind: str
    classification: Optional[DocumentClassification] = None
    document: dict[str, Any]
    document_date: Optional[str] = None
    items: list[dict[str, Any]]
    model: str
    calls: list[dict[str, Any]]


def extract_document(provider: Provider, model: str, pages: list[dict[str, Any]],
                     kind: str | None = None) -> ExtractionResult:
    calls: list[ModelCall] = []
    classification = None
    if kind is None or kind == "auto":
        classification, call = classify(provider, model, pages)
        calls.append(call)
        kind = classification.kind
    doc, call = extract(provider, model, pages, kind)
    calls.append(call)
    data = doc.model_dump()
    return ExtractionResult(
        kind=kind,
        classification=classification,
        document=data,
        document_date=_pick_date(data, classification),
        items=items_from_document(kind, doc),
        model=model,
        calls=[c.as_dict() for c in calls],
    )


def _pick_date(data: dict[str, Any], classification: DocumentClassification | None) -> str | None:
    for key in ("document_date", "study_date"):
        if data.get(key):
            return parse_date(data[key]) and data[key]
    if classification and classification.document_date:
        return classification.document_date
    return None


_DATE_PATTERNS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d")


def parse_date(value: Any) -> date | None:
    """Best-effort date parse for dates as written on documents. None if unsure."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _clamp(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:80]


def items_from_document(kind: str, doc: BaseModel) -> list[dict[str, Any]]:
    """Flatten a document into review items: {page_number, item_type, payload, confidence}."""
    items: list[dict[str, Any]] = []
    if isinstance(doc, PrescriptionDocument):
        for m in doc.medications:
            items.append({
                "page_number": m.page, "item_type": "medication", "confidence": _clamp(m.confidence),
                "payload": {
                    "name": m.name, "generic_name": m.generic_name, "dose": m.dose, "route": m.route,
                    "frequency": m.frequency, "duration": m.duration, "instructions": m.instructions,
                    "prescriber": doc.prescriber, "clinic": doc.clinic, "start_date": doc.document_date,
                    "low_confidence_fields": m.low_confidence_fields,
                },
            })
        for d in doc.diagnoses:
            items.append({"page_number": 1, "item_type": "diagnosis", "confidence": _clamp(doc.overall_confidence),
                          "payload": {"text": d, "prescriber": doc.prescriber, "date": doc.document_date,
                                      "low_confidence_fields": []}})
    elif isinstance(doc, LabReportDocument):
        for v in doc.values:
            items.append({
                "page_number": v.page, "item_type": "lab_value", "confidence": _clamp(v.confidence),
                "payload": {
                    "test_name": v.test_name, "reading_type": "lab_" + slug(v.test_name), "value_text": v.value_text,
                    "numeric_value": v.numeric_value, "unit": v.unit, "reference_range": v.reference_range,
                    "flag": v.flag, "collected_at": v.collected_at or doc.document_date, "lab_name": doc.lab_name,
                    "low_confidence_fields": v.low_confidence_fields,
                },
            })
    elif isinstance(doc, ImagingReportDocument):
        for f in doc.items:
            items.append({
                "page_number": f.page, "item_type": "imaging_finding", "confidence": _clamp(f.confidence),
                "payload": {"text": f.text, "section": f.section, "modality": doc.modality, "body_part": doc.body_part,
                            "study_date": doc.study_date, "radiologist": doc.radiologist, "low_confidence_fields": []},
            })
    elif isinstance(doc, GenericDocument):
        for f in doc.facts:
            items.append({"page_number": f.page, "item_type": "fact", "confidence": _clamp(f.confidence),
                          "payload": {"label": f.label, "value": f.value, "date": doc.document_date,
                                      "low_confidence_fields": []}})
        for name in doc.medications_mentioned:
            items.append({"page_number": 1, "item_type": "medication", "confidence": _clamp(doc.overall_confidence) * 0.8,
                          "payload": {"name": name, "generic_name": None, "dose": None, "route": None, "frequency": None,
                                      "duration": None, "instructions": None, "prescriber": doc.author, "clinic": None,
                                      "start_date": doc.document_date, "low_confidence_fields": ["dose", "frequency"]}})
    return items
