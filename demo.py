"""Demo provider: plausible canned outputs for every schema, so the app can be
tried end to end without a model key. Enable with MODEL_PROVIDER=demo.
Everything it returns is made up and is labeled as such in the output.
"""
from __future__ import annotations

from typing import Any

from providers import FakeProvider, Part


def _schema_response(schema_name: str, parts: list[Part]) -> dict[str, Any]:
    text = " ".join(p.text or "" for p in parts)
    if schema_name == "DocumentClassification":
        kind = "lab_report" if "lab" in text.lower() else "prescription"
        return {"kind": kind, "confidence": 0.8, "patient_name": None, "document_date": "2026-09-01", "language": "en",
                "reason": "demo mode: no model key configured"}
    if schema_name == "PrescriptionDocument":
        return {"prescriber": "Dr. Demo", "clinic": "Demo Clinic", "patient_name": None, "document_date": "2026-09-01",
                "diagnoses": ["(demo) Type 2 diabetes"], "notes": "Demo output, not read from the document.", "overall_confidence": 0.5,
                "medications": [
                    {"name": "Metformin (demo)", "generic_name": "metformin", "dose": "500 mg", "route": "oral", "frequency": "twice daily",
                     "duration": None, "instructions": "after meals", "page": 1, "confidence": 0.9, "low_confidence_fields": []},
                    {"name": "Amlodipine (demo)", "generic_name": None, "dose": "5 mg", "route": "oral", "frequency": "once daily",
                     "duration": None, "instructions": None, "page": 1, "confidence": 0.55, "low_confidence_fields": ["dose"]}]}
    if schema_name == "LabReportDocument":
        return {"lab_name": "Demo Lab", "ordering_doctor": None, "patient_name": None, "document_date": "2026-09-05", "comments": None,
                "overall_confidence": 0.5,
                "values": [{"test_name": "HbA1c (demo)", "value_text": "7.8", "numeric_value": 7.8, "unit": "%", "reference_range": "4.0-5.6",
                            "flag": "high", "collected_at": None, "page": 1, "confidence": 0.9, "low_confidence_fields": []},
                           {"test_name": "Creatinine (demo)", "value_text": "1.3", "numeric_value": 1.3, "unit": "mg/dL", "reference_range": "0.6-1.2",
                            "flag": "high", "collected_at": None, "page": 1, "confidence": 0.7, "low_confidence_fields": ["unit"]}]}
    if schema_name == "ImagingReportDocument":
        return {"modality": "X-ray (demo)", "body_part": "chest", "study_date": "2026-08-20", "radiologist": None, "referring_doctor": None,
                "patient_name": None, "overall_confidence": 0.5,
                "items": [{"text": "(demo) No acute abnormality reported.", "section": "impression", "page": 1, "confidence": 0.8}]}
    if schema_name == "GenericDocument":
        return {"title": "Demo document", "author": None, "patient_name": None, "document_date": None, "overall_confidence": 0.4,
                "summary": "Demo mode: the document was stored but not read.", "facts": [], "medications_mentioned": []}
    if schema_name == "RoutingDecision":
        return {"agents": ["general_duty"], "reason": "demo"}
    if schema_name == "SpecialistOutput":
        agent = "specialist"
        for line in text.splitlines():
            pass
        return {"agent_type": agent, "summary": "(demo) The records on file were listed in the context; no model read them.",
                "observations": [{"text": "(demo) This is placeholder output. Add a model key to get real summaries.",
                                  "category": "other", "sources": [{"file_id": None, "page": None, "label": "demo"}], "confidence": 0.1}],
                "questions_for_doctor": [{"question": "(demo) Is the current medication list complete?", "for_doctor": "treating doctor",
                                          "why": "demo", "priority": "low"}],
                "missing_records": [], "cross_domain_flags": [], "declined_advice_request": False, "decline_note": None, "confidence": 0.1}
    if schema_name == "CoordinatorOutput":
        return {"headline": "(demo) Placeholder brief: no model key is configured.",
                "what_changed": ["(demo) Nothing was analyzed."],
                "key_observations": [{"text": "(demo) Add OPENAI_API_KEY to get real output.", "category": "other",
                                      "sources": [{"file_id": None, "page": None, "label": "demo"}], "confidence": 0.1}],
                "questions_by_doctor": [{"doctor": "Treating doctor", "questions": ["(demo) Is the medication list complete?"]}],
                "missing_records": [], "disagreements": [], "declined_advice_request": False, "decline_note": None}
    raise KeyError(schema_name)


class DemoProvider(FakeProvider):
    name = "demo"

    def __init__(self):
        super().__init__(name="demo", input_tokens=0, output_tokens=0)

    def structured(self, *, model, system, parts, schema, purpose, max_tokens=4000, temperature=0.0):
        call = self._record(model, purpose, system, parts, schema)
        return schema.model_validate(_schema_response(schema.__name__, parts)), call

    def text(self, *, model, system, parts, purpose, max_tokens=2000, temperature=0.0):
        call = self._record(model, purpose, system, parts, None)
        return "(demo) No model key configured.", call
