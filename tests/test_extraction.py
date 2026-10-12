"""Extraction tests with a fake provider. No database, no network."""
import io
from datetime import date

import pytest

import extraction
from extraction import (DocumentClassification, GenericDocument, ImagingReportDocument, LabReportDocument,
                        PrescriptionDocument)
from providers import FakeProvider, ModelCall, Part, ProviderError, estimate_cost, get_provider


def make_pdf(texts):
    import pymupdf
    doc = pymupdf.open()
    for t in texts:
        page = doc.new_page()
        page.insert_text((72, 72), t)
    return doc.tobytes()


def make_png(size=(300, 200)):
    from PIL import Image
    img = Image.new("RGB", size, "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


RX = {
    "prescriber": "Dr. Khan", "clinic": "City Clinic", "patient_name": "Parent One", "document_date": "2026-09-01",
    "diagnoses": ["Type 2 diabetes"], "notes": None, "overall_confidence": 0.9,
    "medications": [
        {"name": "Metformin", "generic_name": "metformin", "dose": "500 mg", "route": "oral", "frequency": "twice daily",
         "duration": "3 months", "instructions": "after meals", "page": 1, "confidence": 0.95, "low_confidence_fields": []},
        {"name": "Amlod?ne", "generic_name": None, "dose": "5 mg", "route": None, "frequency": "once daily", "duration": None,
         "instructions": None, "page": 2, "confidence": 0.4, "low_confidence_fields": ["name"]},
    ],
}

LAB = {
    "lab_name": "Chughtai Lab", "ordering_doctor": None, "patient_name": "Parent One", "document_date": "05/09/2026",
    "comments": None, "overall_confidence": 0.85,
    "values": [
        {"test_name": "HbA1c", "value_text": "7.8", "numeric_value": 7.8, "unit": "%", "reference_range": "4.0-5.6",
         "flag": "high", "collected_at": None, "page": 1, "confidence": 0.9, "low_confidence_fields": []},
        {"test_name": "Creatinine", "value_text": "1.4", "numeric_value": 1.4, "unit": "mg/dL", "reference_range": "0.6-1.2",
         "flag": "high", "collected_at": "2026-09-04", "page": 1, "confidence": 0.8, "low_confidence_fields": ["unit"]},
        {"test_name": "Culture", "value_text": "No growth", "numeric_value": None, "unit": None, "reference_range": None,
         "flag": "normal", "collected_at": None, "page": 2, "confidence": 0.7, "low_confidence_fields": []},
    ],
}


def test_render_pdf_pages_and_text():
    pages = extraction.render_pages(make_pdf(["Hello page one", "Second page"]), "application/pdf", "x.pdf")
    assert [p["page_number"] for p in pages] == [1, 2]
    assert pages[0]["image_png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert "Hello page one" in pages[0]["text"]
    assert pages[0]["width"] > 0 and pages[0]["height"] > 0


def test_render_pdf_detects_by_magic_bytes_and_caps_pages():
    pdf = make_pdf([f"p{i}" for i in range(extraction.MAX_PAGES + 3)])
    pages = extraction.render_pages(pdf, "application/octet-stream", "unknown.bin")
    assert len(pages) == extraction.MAX_PAGES


def test_render_image_normalizes_to_png_and_shrinks():
    big = make_png((4000, 1000))
    pages = extraction.render_pages(big, "image/png", "photo.png")
    assert len(pages) == 1
    assert max(pages[0]["width"], pages[0]["height"]) <= extraction.MAX_IMAGE_SIDE
    assert pages[0]["text"] is None


def test_classify_then_extract_prescription():
    fake = FakeProvider(responses={
        DocumentClassification: {"kind": "prescription", "confidence": 0.9, "patient_name": "Parent One",
                                 "document_date": "2026-09-01", "language": "en", "reason": "Rx header"},
        PrescriptionDocument: RX,
    })
    pages = extraction.render_pages(make_pdf(["Rx", "page 2"]), "application/pdf")
    result = extraction.extract_document(fake, "gpt-4o", pages, kind=None)
    assert result.kind == "prescription"
    assert [c["purpose"] for c in result.calls] == ["classify", "extract:prescription"]
    assert result.document_date == "2026-09-01"
    meds = [i for i in result.items if i["item_type"] == "medication"]
    assert len(meds) == 2
    assert meds[0]["payload"]["name"] == "Metformin"
    assert meds[0]["payload"]["prescriber"] == "Dr. Khan"
    assert meds[0]["page_number"] == 1 and meds[1]["page_number"] == 2
    assert meds[1]["confidence"] == 0.4 and meds[1]["payload"]["low_confidence_fields"] == ["name"]
    assert [i for i in result.items if i["item_type"] == "diagnosis"][0]["payload"]["text"] == "Type 2 diabetes"
    # The image of every page went to the model, with the embedded text.
    extract_call = fake.calls[1]
    assert sum(1 for p in extract_call["parts"] if p.image_png) == 2
    assert "never add medical advice" in extract_call["system"].lower()


def test_extract_lab_report_with_known_kind_skips_classification():
    fake = FakeProvider(responses={LabReportDocument: LAB})
    result = extraction.extract_document(fake, "gpt-4o", [{"page_number": 1, "image_png": make_png(), "text": None}], kind="lab_report")
    assert [c["purpose"] for c in result.calls] == ["extract:lab_report"]
    labs = [i for i in result.items if i["item_type"] == "lab_value"]
    assert [l["payload"]["reading_type"] for l in labs] == ["lab_hba1c", "lab_creatinine", "lab_culture"]
    assert labs[0]["payload"]["collected_at"] == "05/09/2026"   # falls back to the document date
    assert labs[1]["payload"]["collected_at"] == "2026-09-04"
    assert labs[2]["payload"]["numeric_value"] is None
    assert result.document_date == "05/09/2026"


def test_imaging_and_generic_items():
    fake = FakeProvider(responses={
        ImagingReportDocument: {"modality": "MRI", "body_part": "brain", "study_date": "2026-08-20", "radiologist": "Dr. R",
                                "referring_doctor": None, "patient_name": None, "overall_confidence": 0.8,
                                "items": [{"text": "Old lacunar infarct", "section": "finding", "page": 1, "confidence": 0.9},
                                          {"text": "No acute change", "section": "impression", "page": 1, "confidence": 0.9}]},
        GenericDocument: {"title": "Discharge", "author": "Dr. D", "patient_name": None, "document_date": "2026-07-01",
                          "summary": "Admitted for pneumonia", "overall_confidence": 0.7,
                          "facts": [{"label": "Admission", "value": "2026-06-25", "page": 1, "confidence": 0.9}],
                          "medications_mentioned": ["Azithromycin"]},
    })
    img = extraction.extract_document(fake, "m", [{"page_number": 1, "image_png": make_png()}], kind="imaging_report")
    assert [i["item_type"] for i in img.items] == ["imaging_finding", "imaging_finding"]
    assert img.items[0]["payload"]["modality"] == "MRI"
    gen = extraction.extract_document(fake, "m", [{"page_number": 1, "image_png": make_png()}], kind="discharge_summary")
    assert [i["item_type"] for i in gen.items] == ["fact", "medication"]
    assert gen.items[1]["payload"]["name"] == "Azithromycin"
    assert gen.items[1]["payload"]["low_confidence_fields"] == ["dose", "frequency"]


def test_confidence_is_clamped():
    fake = FakeProvider(responses={LabReportDocument: {**LAB, "values": [{**LAB["values"][0], "confidence": 7}]}})
    result = extraction.extract_document(fake, "m", [{"page_number": 1, "image_png": make_png()}], kind="lab_report")
    assert result.items[0]["confidence"] == 1.0


def test_parse_date_formats():
    assert extraction.parse_date("2026-09-01") == date(2026, 9, 1)
    assert extraction.parse_date("05/09/2026") == date(2026, 9, 5)
    assert extraction.parse_date("5 Sep 2026") == date(2026, 9, 5)
    assert extraction.parse_date("Sep 5, 2026") == date(2026, 9, 5)
    assert extraction.parse_date("unknown") is None
    assert extraction.parse_date(None) is None
    assert extraction.parse_date(date(2026, 1, 1)) == date(2026, 1, 1)


def test_fake_provider_without_response_raises():
    with pytest.raises(ProviderError):
        extraction.extract_document(FakeProvider(), "m", [{"page_number": 1, "image_png": make_png()}], kind="lab_report")


def test_cost_estimate_and_registry():
    assert estimate_cost("gpt-4o", 1_000_000, 0) == 2.5
    assert estimate_cost("no-such-model", 10, 10) is None
    assert isinstance(get_provider("fake"), FakeProvider)
    with pytest.raises(ProviderError):
        get_provider("nope")


def test_openai_provider_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_INTEGRATIONS_OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError):
        get_provider("openai")


def test_openai_content_parts_encode_images():
    from providers import OpenAIProvider
    parts = OpenAIProvider._content([Part.of_text("hi"), Part.of_png(b"\x89PNG")])
    assert parts[0] == {"type": "text", "text": "hi"}
    assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_strict_schemas_are_accepted_by_the_openai_converter():
    from openai.lib._pydantic import to_strict_json_schema
    for schema in (DocumentClassification, PrescriptionDocument, LabReportDocument, ImagingReportDocument, GenericDocument):
        s = to_strict_json_schema(schema)
        assert s["additionalProperties"] is False
        assert set(s["required"]) == set(s["properties"])
