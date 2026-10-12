"""FastAPI service layer (item 7). Same service functions the Streamlit UI
calls, exposed over HTTP for the future MCP server and mobile client.

Auth: POST /auth/login with email and password returns a bearer token (HMAC
signed, 12 hours). Every other route needs `Authorization: Bearer <token>`.
Roles are enforced by the service functions themselves.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import appointments as appts
import auth
import board
import brief as brief_mod
import database as db
import documents
import medications as meds
from providers import FakeProvider, ProviderError, get_provider

TOKEN_TTL_SECONDS = 12 * 3600
_SECRET = (os.environ.get("APP_SECRET") or secrets.token_hex(32)).encode()

app = FastAPI(title="Family Medical Board API", version="0.1")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def make_token(user_id: str, ttl: int = TOKEN_TTL_SECONDS) -> str:
    payload = f"{user_id}:{int(time.time()) + ttl}".encode()
    sig = hmac.new(_SECRET, payload, hashlib.sha256).hexdigest().encode()
    return base64.urlsafe_b64encode(payload + b"." + sig).decode()


def read_token(token: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        payload, sig = raw.rsplit(b".", 1)
        if not hmac.compare_digest(hmac.new(_SECRET, payload, hashlib.sha256).hexdigest().encode(), sig):
            return None
        user_id, exp = payload.decode().rsplit(":", 1)
        if int(exp) < time.time():
            return None
        return user_id
    except Exception:
        return None


def current_user(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "bearer token required")
    user_id = read_token(authorization.split(" ", 1)[1].strip())
    if not user_id:
        raise HTTPException(401, "invalid or expired token")
    user = db.get_user(user_id)
    if not user or not user.get("is_active"):
        raise HTTPException(401, "account not active")
    return {k: user.get(k) for k in ("id", "email", "display_name", "role", "language", "is_active")}


PROVIDER_OVERRIDE = None  # tests inject a FakeProvider here


def _provider():
    if PROVIDER_OVERRIDE is not None:
        return PROVIDER_OVERRIDE
    name = os.environ.get("MODEL_PROVIDER")
    if name == "fake":
        return FakeProvider()
    try:
        return get_provider(name)
    except ProviderError:
        return None


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except (ValueError, auth.AuthError) as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/auth/login")
def login(body: LoginIn):
    try:
        user = auth.login(body.email, body.password)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    return {"token": make_token(user["id"]), "user": user}


@app.get("/me")
def me(user: dict = Depends(current_user)):
    return user


@app.get("/patients")
def patients(user: dict = Depends(current_user)):
    return _wrap(db.list_patients, user)


class PatientIn(BaseModel):
    name: str
    age: Optional[int] = None
    sex: Optional[str] = None
    date_of_birth: Optional[str] = None
    allergies: Optional[str] = None
    medical_history: Optional[str] = None
    primary_doctor: Optional[str] = None
    notes: Optional[str] = None


@app.post("/patients", status_code=201)
def create_patient(body: PatientIn, user: dict = Depends(current_user)):
    fields = body.model_dump(exclude_none=True)
    name = fields.pop("name")
    pid = _wrap(db.create_patient, user, name, **fields)
    return db.get_patient(pid, user)


@app.get("/patients/{patient_id}")
def get_patient(patient_id: int, user: dict = Depends(current_user)):
    p = _wrap(db.get_patient, patient_id, user)
    if not p:
        raise HTTPException(404, "no such patient")
    return p


@app.patch("/patients/{patient_id}")
def update_patient(patient_id: int, body: PatientIn, user: dict = Depends(current_user)):
    _wrap(db.update_patient, patient_id, user, **body.model_dump(exclude_none=True))
    return db.get_patient(patient_id, user)


@app.get("/patients/{patient_id}/documents")
def list_documents(patient_id: int, user: dict = Depends(current_user)):
    return _wrap(db.list_documents, patient_id, user)


@app.post("/patients/{patient_id}/documents", status_code=201)
async def upload_document(patient_id: int, file: UploadFile = File(...), kind: Optional[str] = Form(default=None),
                          user: dict = Depends(current_user)):
    data = await file.read()
    return _wrap(documents.ingest_upload, patient_id, user, file.filename or "upload", file.content_type or "application/octet-stream",
                 data, _provider(), kind=kind)


@app.get("/patients/{patient_id}/documents/{file_id}")
def get_document(patient_id: int, file_id: int, user: dict = Depends(current_user)):
    d = _wrap(db.get_document, file_id, patient_id, user)
    if not d:
        raise HTTPException(404, "no such document")
    return d


@app.get("/patients/{patient_id}/documents/{file_id}/pages/{page}.png")
def get_page_image(patient_id: int, file_id: int, page: int, user: dict = Depends(current_user)):
    for p in _wrap(db.get_document_pages, file_id, patient_id, user):
        if p["page_number"] == page and p.get("image_png"):
            return Response(content=bytes(p["image_png"]), media_type="image/png")
    raise HTTPException(404, "no such page")


@app.post("/patients/{patient_id}/documents/{file_id}/extract")
def reextract(patient_id: int, file_id: int, kind: Optional[str] = None, user: dict = Depends(current_user)):
    provider = _provider()
    if provider is None:
        raise HTTPException(503, "no model provider configured")
    return _wrap(documents.run_extraction, patient_id, file_id, user, provider, kind=kind)


@app.get("/patients/{patient_id}/review")
def pending_review(patient_id: int, user: dict = Depends(current_user)):
    return _wrap(db.list_extracted_items, patient_id, user, status="pending")


class ConfirmIn(BaseModel):
    payload: dict[str, Any]


@app.post("/patients/{patient_id}/review/{item_id}/confirm")
def confirm_item(patient_id: int, item_id: int, body: ConfirmIn, user: dict = Depends(current_user)):
    return _wrap(documents.confirm_item, item_id, patient_id, user, body.payload)


@app.post("/patients/{patient_id}/review/{item_id}/reject")
def reject_item(patient_id: int, item_id: int, user: dict = Depends(current_user)):
    _wrap(documents.reject_item, item_id, patient_id, user)
    return {"ok": True}


@app.get("/patients/{patient_id}/medications")
def list_medications(patient_id: int, status: Optional[str] = None, user: dict = Depends(current_user)):
    return _wrap(meds.list_medications, patient_id, user, status=status)


class MedicationIn(BaseModel):
    name: str
    dose: Optional[str] = None
    route: Optional[str] = None
    frequency: Optional[str] = None
    indication: Optional[str] = None
    prescriber: Optional[str] = None
    start_date: Optional[str] = None
    notes: Optional[str] = None


@app.post("/patients/{patient_id}/medications", status_code=201)
def add_medication(patient_id: int, body: MedicationIn, user: dict = Depends(current_user)):
    from extraction import parse_date
    fields = body.model_dump()
    fields["start_date"] = parse_date(fields.get("start_date"))
    mid = _wrap(meds.add_medication, patient_id, user, **fields)
    return {"id": mid}


@app.get("/patients/{patient_id}/medications/history")
def medication_history(patient_id: int, user: dict = Depends(current_user)):
    return _wrap(meds.medication_history, patient_id, user)


@app.get("/patients/{patient_id}/vitals")
def vitals(patient_id: int, reading_type: Optional[str] = None, user: dict = Depends(current_user)):
    return _wrap(db.get_vitals_history, patient_id, user, reading_type=reading_type)


@app.get("/patients/{patient_id}/appointments")
def list_appointments(patient_id: int, upcoming: bool = False, user: dict = Depends(current_user)):
    return _wrap(appts.list_appointments, patient_id, user, upcoming_only=upcoming)


class AppointmentIn(BaseModel):
    title: str
    starts_at: str
    kind: str = "visit"
    doctor_name: Optional[str] = None
    specialty: Optional[str] = None
    location: Optional[str] = None
    reminder_minutes: int = 1440
    notes: Optional[str] = None


@app.post("/patients/{patient_id}/appointments", status_code=201)
def add_appointment(patient_id: int, body: AppointmentIn, user: dict = Depends(current_user)):
    from datetime import datetime
    fields = body.model_dump()
    fields["starts_at"] = datetime.fromisoformat(fields["starts_at"])
    aid = _wrap(appts.add_appointment, patient_id, user, **fields)
    return {"id": aid}


@app.get("/patients/{patient_id}/appointments.ics", response_class=PlainTextResponse)
def appointments_ics(patient_id: int, user: dict = Depends(current_user)):
    rows = _wrap(appts.list_appointments, patient_id, user)
    return PlainTextResponse(appts.to_ics(rows), media_type="text/calendar")


class BoardIn(BaseModel):
    query: str
    language: Optional[str] = None
    force_all: bool = False


@app.post("/patients/{patient_id}/board")
def run_board(patient_id: int, body: BoardIn, user: dict = Depends(current_user)):
    provider = _provider()
    if provider is None:
        raise HTTPException(503, "no model provider configured")
    run = _wrap(board.run_board, patient_id, user, body.query, provider, language=body.language, force_all=body.force_all)
    run["markdown"] = board.format_markdown(run)
    return run


@app.get("/patients/{patient_id}/board")
def list_board_runs(patient_id: int, user: dict = Depends(current_user)):
    return _wrap(board.list_runs, patient_id, user)


@app.get("/patients/{patient_id}/brief")
def get_brief(patient_id: int, appointment_id: Optional[int] = None, format: str = "json", user: dict = Depends(current_user)):
    b = _wrap(brief_mod.build_brief, patient_id, user, appointment_id=appointment_id)
    lang = user.get("language") or "en"
    if format == "md":
        return PlainTextResponse(brief_mod.brief_markdown(b, lang), media_type="text/markdown")
    if format == "pdf":
        return Response(content=brief_mod.brief_pdf(b, lang), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="brief-{patient_id}.pdf"'})
    return json.loads(json.dumps(b, default=str))


@app.get("/audit")
def audit_log(patient_id: Optional[int] = None, user: dict = Depends(current_user)):
    _wrap(db.require_role, user, "admin")
    return db.get_audit_log(patient_id)
