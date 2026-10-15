"""Family Medical Board: Streamlit UI.

Pages: Home, Documents (upload, extract, review and confirm), Medications,
Values, Appointments, Board, Brief, Settings. Every page works from the
patient chosen in the sidebar and the signed-in user's role. The app never
gives medical advice; the board summarizes records and lists questions for
the treating doctor.
"""
from __future__ import annotations

import io
import os
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd
import streamlit as st

import appointments as appts
import auth
import board
import brief as brief_mod
import database as db
import documents
import medications as meds
from extraction import parse_date
from providers import ProviderError, get_provider

APP_TITLE = "Family Medical Board"
DISCLAIMER = "This app keeps records and lists questions for the treating doctors. It never gives medical advice."
KINDS = {"auto": "Detect automatically", "prescription": "Prescription", "lab_report": "Lab report",
         "imaging_report": "Imaging report", "discharge_summary": "Discharge summary or letter", "other": "Other"}
LANGS = {"en": "English", "ur": "Urdu"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Preparing the database...")
def _startup() -> dict:
    return db.run_migrations()


@st.cache_resource
def _provider():
    try:
        return get_provider()
    except ProviderError:
        return None


def _user() -> dict | None:
    return st.session_state.get("user")


def _refresh_user() -> None:
    u = _user()
    if u:
        fresh = db.get_user(u["id"])
        if fresh and fresh.get("is_active"):
            st.session_state.user = {k: fresh.get(k) for k in ("id", "email", "display_name", "role", "language", "is_active")}
        else:
            st.session_state.pop("user", None)


def _can(role: str) -> bool:
    u = _user()
    return bool(u) and db.ROLE_RANK.get(u.get("role", "viewer"), 0) >= db.ROLE_RANK[role]


def _denied(e: Exception) -> None:
    st.error("You do not have access to do that." if isinstance(e, PermissionError) else str(e))


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def render_login() -> None:
    st.title(APP_TITLE)
    st.caption(DISCLAIMER)
    first = auth.is_first_user()
    tab_in, tab_up, tab_forgot = st.tabs(["Sign in", "Create account", "Forgot password"])
    with tab_in:
        with st.form("login"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                try:
                    st.session_state.user = auth.login(email, password)
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))
    with tab_up:
        if first:
            st.info("No accounts exist yet. The first account becomes the admin.")
        else:
            st.info("You need an invite code from the family admin.")
        with st.form("signup"):
            name = st.text_input("Your name", key="signup_name")
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password (at least 10 characters)", type="password", key="signup_password")
            code = "" if first else st.text_input("Invite code", key="signup_code")
            if st.form_submit_button("Create account", type="primary", width="stretch"):
                try:
                    st.session_state.user = auth.signup(email, password, name, code or None)
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))
    with tab_forgot:
        if auth.reset_secret_configured():
            st.info("Enter the family reset code (the admin keeps it) and choose a new password.")
        else:
            st.info("Ask the family admin to reset your password from Settings. Admins can enable self-service "
                    "reset by adding a PASSWORD_RESET_SECRET secret to the app.")
        with st.form("forgot"):
            email = st.text_input("Email", key="forgot_email")
            code = st.text_input("Family reset code", type="password", key="forgot_code")
            password = st.text_input("New password (at least 10 characters)", type="password", key="forgot_password")
            if st.form_submit_button("Set new password", width="stretch"):
                try:
                    st.session_state.user = auth.reset_password_with_secret(email, code, password)
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar() -> int | None:
    u = _user()
    with st.sidebar:
        st.markdown(f"**{u['display_name']}**  \n{u['role']}")
        patients = db.list_patients(u)
        options = {p["id"]: p["name"] for p in patients}
        current = st.session_state.get("current_patient_id")
        if current not in options:
            current = next(iter(options), None)
        if options:
            chosen = st.selectbox("Patient", list(options), index=list(options).index(current) if current else 0,
                                  format_func=lambda i: options[i], key="patient_select")
            st.session_state.current_patient_id = chosen
        else:
            st.session_state.current_patient_id = None
            st.caption("No patients yet. Add one on the Home page.")
        if _can("editor"):
            with st.expander("Add patient"):
                with st.form("add_patient", clear_on_submit=True):
                    name = st.text_input("Name")
                    age = st.number_input("Age", 0, 120, 75)
                    if st.form_submit_button("Add"):
                        if name.strip():
                            try:
                                st.session_state.current_patient_id = db.create_patient(u, name.strip(), age=int(age))
                                st.rerun()
                            except Exception as e:
                                _denied(e)
        st.divider()
        if _provider() is None:
            st.warning("No model key configured. Uploads are stored but not extracted, and the board cannot run. "
                       "Set OPENAI_API_KEY.")
        if st.button("Sign out", width="stretch"):
            for k in ("user", "current_patient_id", "conversation_id"):
                st.session_state.pop(k, None)
            st.rerun()
        st.caption(DISCLAIMER)
    return st.session_state.get("current_patient_id")


def _need_patient() -> int | None:
    pid = st.session_state.get("current_patient_id")
    if not pid:
        st.info("Add or select a patient in the sidebar.")
    return pid


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------

def page_home() -> None:
    u = _user()
    st.title(APP_TITLE)
    due = appts.due_reminders(u)
    if due:
        for a in due:
            st.warning(f"Reminder: {a['patient_name']}, {a['title']} on {_fmt(a['starts_at'])}"
                       + (f" with {a['doctor_name']}" if a.get("doctor_name") else ""))
    pid = _need_patient()
    if not pid:
        return
    patient = db.get_patient(pid, u)
    st.subheader(patient["name"])
    c1, c2, c3, c4 = st.columns(4)
    pending = sum(len(v) for v in documents.pending_review(pid, u).values())
    c1.metric("Items awaiting review", pending)
    c2.metric("Active medications", len(meds.list_medications(pid, u, status="active")))
    c3.metric("Documents", len(db.list_documents(pid, u)))
    upcoming = appts.list_appointments(pid, u, upcoming_only=True, limit=5)
    c4.metric("Upcoming appointments", len(upcoming))
    if pending:
        st.info(f"{pending} extracted items are waiting for someone to check them against the original. Open Documents.")
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Latest values**")
        latest = db.get_latest_vitals(pid, u)
        if latest:
            rows = [{"Value": v["reading_type"].replace("lab_", "").replace("_", " "), "Result": f"{v['value']} {v['unit'] or ''}",
                     "Date": _fmt(v["recorded_at"] or v["timestamp"])[:10], "Flag": v.get("flag") or "",
                     "Change": f"{v['change_from_previous']:+.1f}" if v.get("change_from_previous") is not None else ""} for v in latest]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("Nothing recorded yet.")
    with col_b:
        st.markdown("**Upcoming appointments**")
        if upcoming:
            for a in upcoming:
                st.write(f"{_fmt(a['starts_at'])}: {a['title']}" + (f" with {a['doctor_name']}" if a.get("doctor_name") else ""))
        else:
            st.caption("None scheduled.")
    with st.expander("Patient details", expanded=False):
        _patient_form(patient)


def _patient_form(patient: dict) -> None:
    u = _user()
    disabled = not _can("editor")
    with st.form("patient_form"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Name", patient["name"], disabled=disabled)
        age = c1.number_input("Age", 0, 120, int(patient.get("age") or 0), disabled=disabled)
        dob = c1.date_input("Date of birth", patient.get("date_of_birth"), disabled=disabled, min_value=date(1900, 1, 1))
        sex = c1.text_input("Sex", patient.get("sex") or "", disabled=disabled)
        primary = c1.text_input("Primary doctor", patient.get("primary_doctor") or "", disabled=disabled)
        allergies = c2.text_area("Allergies", patient.get("allergies") or "", disabled=disabled)
        history = c2.text_area("Medical history", patient.get("medical_history") or "", disabled=disabled)
        notes = c2.text_area("Notes", patient.get("notes") or "", disabled=disabled)
        if st.form_submit_button("Save", disabled=disabled):
            try:
                db.update_patient(patient["id"], u, name=name, age=int(age), date_of_birth=dob, sex=sex or None,
                                  primary_doctor=primary or None, allergies=allergies, medical_history=history, notes=notes)
                st.success("Saved.")
            except Exception as e:
                _denied(e)
    if _can("admin"):
        if st.button("Archive this patient", key="archive_patient"):
            db.archive_patient(patient["id"], u)
            st.session_state.current_patient_id = None
            st.rerun()


# ---------------------------------------------------------------------------
# Documents: upload, review, confirm
# ---------------------------------------------------------------------------

def page_documents() -> None:
    u = _user()
    pid = _need_patient()
    if not pid:
        return
    st.title("Documents")
    provider = _provider()
    if _can("editor"):
        st.subheader("Add documents")
        kind = st.selectbox("What is it?", list(KINDS), format_func=lambda k: KINDS[k], key="upload_kind")
        c1, c2 = st.columns(2)
        files = c1.file_uploader("Photos or PDFs", type=["pdf", "jpg", "jpeg", "png", "webp", "dcm"], accept_multiple_files=True, key="uploader")
        shot = c2.camera_input("Or take a photo", key="camera")
        pending_uploads = list(files or [])
        if shot is not None:
            pending_uploads.append(shot)
        if pending_uploads and st.button("Upload and extract", type="primary", key="upload_btn"):
            for f in pending_uploads:
                data = f.getvalue()
                name = getattr(f, "name", None) or f"photo-{datetime.now():%Y%m%d-%H%M%S}.jpg"
                mime = getattr(f, "type", None) or "application/octet-stream"
                with st.spinner(f"Processing {name}..."):
                    try:
                        out = documents.ingest_upload(pid, u, name, mime, data, provider, kind=kind)
                    except Exception as e:
                        _denied(e)
                        continue
                if out.get("error"):
                    st.error(f"{name}: stored, but extraction failed: {out['error']}")
                elif provider is None:
                    st.warning(f"{name}: stored ({out['pages']} pages). No model key, so nothing was extracted.")
                else:
                    st.success(f"{name}: {out['pages']} pages, {out['items']} items found as {KINDS.get(out['kind'], out['kind'])}. Review them below.")
            st.rerun()
    st.divider()
    _review_section(pid)
    st.divider()
    _document_list(pid, provider)


def _review_section(pid: int) -> None:
    u = _user()
    grouped = documents.pending_review(pid, u)
    st.subheader("Check extracted items against the original")
    if not grouped:
        st.caption("Nothing waiting for review.")
        return
    st.caption("Nothing is saved to the record until you confirm it here. Correct any field first if needed.")
    editable = _can("editor")
    for file_id, items in grouped.items():
        doc = db.get_document(file_id, pid, u)
        pages = {p["page_number"]: p for p in db.get_document_pages(file_id, pid, u)}
        with st.expander(f"{doc['filename']} ({KINDS.get(doc.get('document_kind'), 'document')}, {len(items)} items)", expanded=True):
            if editable:
                high = [i for i in items if (i.get("confidence") or 0) >= 0.85 and _confirmable(i)]
                if high and st.button(f"Confirm all {len(high)} high-confidence items", key=f"confirm_all_{file_id}"):
                    for it in high:
                        try:
                            documents.confirm_item(it["id"], pid, u, it["payload"])
                        except Exception as e:
                            _denied(e)
                    st.rerun()
            for it in items:
                left, right = st.columns([1, 1])
                page = pages.get(it.get("page_number") or 1)
                with left:
                    if page and page.get("image_png"):
                        st.image(bytes(page["image_png"]), caption=f"Page {it.get('page_number') or 1}", width="stretch")
                    else:
                        st.caption("No page image.")
                with right:
                    _item_editor(it, pid, editable)
                st.markdown("---")


def _confirmable(item: dict) -> bool:
    if item["item_type"] == "lab_value":
        return item["payload"].get("numeric_value") is not None
    if item["item_type"] == "medication":
        return bool(item["payload"].get("name"))
    return True


def _item_editor(it: dict, pid: int, editable: bool) -> None:
    u = _user()
    p = dict(it["payload"])
    conf = it.get("confidence") or 0
    low = set(p.get("low_confidence_fields") or [])
    badge = "high" if conf >= 0.85 else ("medium" if conf >= 0.6 else "low")
    st.markdown(f"**{it['item_type'].replace('_', ' ').title()}**, confidence {conf:.0%} ({badge})")
    if low:
        st.caption("Check these fields: " + ", ".join(sorted(low)))
    key = f"item_{it['id']}"
    with st.form(key):
        new = dict(p)
        if it["item_type"] == "medication":
            for f in ("name", "generic_name", "dose", "route", "frequency", "prescriber", "start_date", "instructions"):
                label = f.replace("_", " ").title() + (" (unsure)" if f in low else "")
                new[f] = st.text_input(label, p.get(f) or "", key=f"{key}_{f}") or None
        elif it["item_type"] == "lab_value":
            new["test_name"] = st.text_input("Test" + (" (unsure)" if "test_name" in low else ""), p.get("test_name") or "", key=f"{key}_test")
            new["numeric_value"] = st.number_input("Value" + (" (unsure)" if "numeric_value" in low else ""),
                                                   value=float(p["numeric_value"]) if p.get("numeric_value") is not None else 0.0,
                                                   format="%.3f", key=f"{key}_val")
            new["unit"] = st.text_input("Unit" + (" (unsure)" if "unit" in low else ""), p.get("unit") or "", key=f"{key}_unit") or None
            new["reference_range"] = st.text_input("Reference range", p.get("reference_range") or "", key=f"{key}_ref") or None
            new["flag"] = st.selectbox("Flag", ["normal", "high", "low", "abnormal", "unknown"],
                                       index=["normal", "high", "low", "abnormal", "unknown"].index(p.get("flag") or "unknown"), key=f"{key}_flag")
            new["collected_at"] = st.text_input("Date", p.get("collected_at") or "", key=f"{key}_date") or None
            if p.get("value_text"):
                st.caption(f"As printed: {p['value_text']}")
        else:
            text = p.get("text") or (f"{p.get('label', '')}: {p.get('value', '')}" if p.get("label") else "")
            new["text"] = st.text_area("Text", text, key=f"{key}_text")
            if p.get("section"):
                st.caption(f"Section: {p['section']}")
        c1, c2 = st.columns(2)
        ok = c1.form_submit_button("Confirm", type="primary", disabled=not editable, width="stretch")
        no = c2.form_submit_button("Reject", disabled=not editable, width="stretch")
    if ok:
        try:
            documents.confirm_item(it["id"], pid, u, new)
            st.rerun()
        except Exception as e:
            _denied(e)
    if no:
        try:
            documents.reject_item(it["id"], pid, u)
            st.rerun()
        except Exception as e:
            _denied(e)


def _document_list(pid: int, provider) -> None:
    u = _user()
    st.subheader("All documents")
    docs = db.list_documents(pid, u)
    if not docs:
        st.caption("No documents yet.")
        return
    for d in docs:
        label = f"{_fmt(d['uploaded_at'])[:10]}  {d['filename']}  ({KINDS.get(d.get('document_kind'), 'unknown kind')}, {d['status']})"
        with st.expander(label):
            c1, c2, c3 = st.columns(3)
            c1.write(f"Pages: {d.get('page_count') or 0}")
            c2.write(f"Document date: {_fmt(d.get('document_date')) or 'unknown'}")
            c3.write(f"Model: {d.get('extraction_model') or 'none'}")
            if d.get("extraction_error"):
                st.error(d["extraction_error"])
            pages = db.get_document_pages(d["id"], pid, u)
            cols = st.columns(min(3, max(1, len(pages))))
            for i, pg in enumerate(pages[:6]):
                if pg.get("image_png"):
                    cols[i % len(cols)].image(bytes(pg["image_png"]), caption=f"Page {pg['page_number']}", width="stretch")
            full = db.get_document(d["id"], pid, u, with_data=True)
            if full and full.get("file_data"):
                st.download_button("Download original", bytes(full["file_data"]), file_name=d["filename"], key=f"dl_{d['id']}")
            if _can("editor"):
                b1, b2 = st.columns(2)
                if provider is not None and b1.button("Extract again", key=f"re_{d['id']}"):
                    with st.spinner("Extracting..."):
                        out = documents.run_extraction(pid, d["id"], u, provider, kind=None)
                    st.success(f"{out['items']} items queued for review." if not out.get("error") else out["error"])
                    st.rerun()
                if b2.button("Delete document", key=f"del_{d['id']}"):
                    db.delete_document(d["id"], pid, u)
                    st.rerun()


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------

def page_medications() -> None:
    u = _user()
    pid = _need_patient()
    if not pid:
        return
    st.title("Medications")
    found = meds.find_duplicates(pid, u)
    for g in found.get("duplicates", []):
        st.warning("Possible duplicate: " + ", ".join(f"{m['name']} ({m.get('prescriber') or 'unknown prescriber'})" for m in g))
    for mp in found.get("multi_prescriber", []):
        st.warning(f"{mp['name']} appears from more than one prescriber: {', '.join(mp['prescribers'])}")
    active = meds.list_medications(pid, u, status="active")
    if active:
        rows = [{"Name": m["name"], "Dose": m.get("dose") or "", "Frequency": m.get("frequency") or "", "Route": m.get("route") or "",
                 "Prescriber": m.get("prescriber") or "", "Since": _fmt(m.get("start_date")), "Source": f"doc {m['source_file_id']} p{m['source_page']}" if m.get("source_file_id") else "manual"}
                for m in active]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("No active medications recorded. Upload a prescription or add one below.")
    if _can("editor"):
        c1, c2 = st.columns(2)
        with c1, st.form("add_med", clear_on_submit=True):
            st.markdown("**Add a medication**")
            name = st.text_input("Name")
            dose = st.text_input("Dose")
            freq = st.text_input("Frequency")
            presc = st.text_input("Prescriber")
            start = st.date_input("Start date", value=None)
            if st.form_submit_button("Add") and name.strip():
                try:
                    meds.add_medication(pid, u, name.strip(), dose=dose or None, frequency=freq or None, prescriber=presc or None, start_date=start)
                    st.rerun()
                except Exception as e:
                    _denied(e)
        with c2, st.form("stop_med"):
            st.markdown("**Stop a medication**")
            opts = {m["id"]: f"{m['name']} {m.get('dose') or ''}" for m in active}
            mid = st.selectbox("Medication", list(opts), format_func=lambda i: opts[i]) if opts else None
            reason = st.text_input("Reason (as told by the doctor)")
            if st.form_submit_button("Mark stopped") and mid:
                try:
                    meds.stop_medication(mid, pid, u, reason=reason or None)
                    st.rerun()
                except Exception as e:
                    _denied(e)
    with st.expander("History of changes"):
        hist = meds.medication_history(pid, u)
        if hist:
            rows = [{"When": _fmt(h["changed_at"]), "Medication": h.get("medication_name") or "", "Change": h["change_type"],
                     "By": h.get("changed_by_name") or "", "After": ", ".join(f"{k}: {v}" for k, v in (h.get("after") or {}).items() if v)} for h in hist]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.caption("No changes yet.")
    stopped = meds.list_medications(pid, u, status="stopped")
    if stopped:
        with st.expander("Stopped medications"):
            st.dataframe(pd.DataFrame([{"Name": m["name"], "Dose": m.get("dose") or "", "Stopped": _fmt(m.get("end_date")), "Notes": m.get("notes") or ""} for m in stopped]),
                         hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Values (vitals and labs)
# ---------------------------------------------------------------------------

def page_values() -> None:
    u = _user()
    pid = _need_patient()
    if not pid:
        return
    st.title("Values")
    history = db.get_vitals_history(pid, u)
    if history:
        df = pd.DataFrame(history)
        df["when"] = df["recorded_at"].where(df["recorded_at"].notna(), df["timestamp"])
        types = sorted(df["reading_type"].unique())
        chosen = st.selectbox("Show", types, format_func=lambda t: t.replace("lab_", "").replace("_", " "))
        sub = df[df["reading_type"] == chosen].sort_values("when")
        if len(sub) > 1:
            st.line_chart(sub.set_index("when")["value"])
        table = sub.sort_values("when", ascending=False)[["when", "value", "unit", "reference_range", "flag", "source", "source_page"]].copy()
        table["when"] = table["when"].apply(lambda v: _fmt(v))
        table.columns = ["When", "Value", "Unit", "Reference", "Flag", "Source", "Page"]
        st.dataframe(table, hide_index=True, width="stretch")
    else:
        st.caption("No values yet. Upload a lab report or add one below.")
    if _can("editor"):
        with st.form("add_value", clear_on_submit=True):
            st.markdown("**Add a reading by hand**")
            c1, c2, c3, c4 = st.columns(4)
            rtype = c1.selectbox("Type", ["blood_pressure_systolic", "blood_pressure_diastolic", "heart_rate", "glucose", "weight",
                                          "oxygen_saturation", "temperature", "other"])
            other = c1.text_input("If other, name")
            value = c2.number_input("Value", step=0.1, format="%.1f")
            unit = c3.text_input("Unit", "mmHg" if "pressure" in rtype else "")
            when = c4.date_input("Date", date.today())
            if st.form_submit_button("Save"):
                name = other.strip().lower().replace(" ", "_") if rtype == "other" and other.strip() else rtype
                try:
                    db.save_vital_reading(pid, u, name, float(value), unit or None, recorded_at=datetime.combine(when, dtime(0, 0)))
                    st.rerun()
                except Exception as e:
                    _denied(e)


# ---------------------------------------------------------------------------
# Appointments
# ---------------------------------------------------------------------------

def page_appointments() -> None:
    u = _user()
    pid = _need_patient()
    if not pid:
        return
    st.title("Appointments and tests")
    rows = appts.list_appointments(pid, u)
    upcoming = [a for a in rows if a["status"] == "scheduled" and a["starts_at"] >= datetime.now() - timedelta(hours=1)]
    past = [a for a in rows if a not in upcoming]
    st.subheader("Upcoming")
    if upcoming:
        for a in upcoming:
            c1, c2, c3 = st.columns([4, 1, 1])
            c1.write(f"**{_fmt(a['starts_at'])}** {a['title']}" + (f" with {a['doctor_name']}" if a.get("doctor_name") else "")
                     + (f" ({a['specialty']})" if a.get("specialty") else "") + (f", {a['location']}" if a.get("location") else ""))
            if _can("editor"):
                if c2.button("Done", key=f"done_{a['id']}"):
                    appts.update_appointment(a["id"], pid, u, status="completed")
                    st.rerun()
                if c3.button("Cancel", key=f"cancel_{a['id']}"):
                    appts.update_appointment(a["id"], pid, u, status="cancelled")
                    st.rerun()
    else:
        st.caption("Nothing scheduled.")
    if rows:
        st.download_button("Download calendar (.ics)", appts.to_ics(upcoming or rows), file_name="appointments.ics", mime="text/calendar")
    if _can("editor"):
        with st.form("add_appt", clear_on_submit=True):
            st.markdown("**Add an appointment or test**")
            c1, c2 = st.columns(2)
            title = c1.text_input("Title")
            kind = c1.selectbox("Kind", ["visit", "test", "procedure", "other"])
            doctor = c1.text_input("Doctor")
            spec = c1.text_input("Specialty")
            day = c2.date_input("Date", date.today() + timedelta(days=1))
            tm = c2.time_input("Time", dtime(10, 0))
            loc = c2.text_input("Location")
            rem = c2.selectbox("Remind", [60, 180, 1440, 2880, 10080], index=2,
                               format_func=lambda m: {60: "1 hour before", 180: "3 hours before", 1440: "1 day before", 2880: "2 days before", 10080: "1 week before"}[m])
            notes = st.text_area("Notes")
            if st.form_submit_button("Add") and title.strip():
                try:
                    appts.add_appointment(pid, u, title.strip(), datetime.combine(day, tm), kind=kind, doctor_name=doctor or None,
                                          specialty=spec or None, location=loc or None, reminder_minutes=int(rem), notes=notes or None)
                    st.rerun()
                except Exception as e:
                    _denied(e)
    if past:
        with st.expander("Past and cancelled"):
            st.dataframe(pd.DataFrame([{"When": _fmt(a["starts_at"]), "Title": a["title"], "Doctor": a.get("doctor_name") or "", "Status": a["status"]} for a in past]),
                         hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------

def page_board() -> None:
    u = _user()
    pid = _need_patient()
    if not pid:
        return
    st.title("Ask the board")
    st.caption("The board reads the confirmed records and tells you what they show and what to ask the doctors. It does not give advice.")
    provider = _provider()
    convs = db.get_user_conversations(u, pid)
    conv_id = st.session_state.get("conversation_id")
    if conv_id not in {c["id"] for c in convs}:
        conv_id = convs[0]["id"] if convs else None
    c1, c2 = st.columns([3, 1])
    if convs:
        labels = {c["id"]: f"{c['title'][:40]} ({_fmt(c['updated_at'])[:10]})" for c in convs}
        conv_id = c1.selectbox("Conversation", list(labels), index=list(labels).index(conv_id), format_func=lambda i: labels[i], key="conv_select")
    if c2.button("New conversation", width="stretch"):
        conv_id = db.create_conversation(u, pid, "New conversation")
        st.session_state.conversation_id = conv_id
        st.rerun()
    st.session_state.conversation_id = conv_id
    force_all = st.toggle("Ask every active specialist (costs more)", value=False, key="force_all")
    lang = u.get("language") or "en"
    if conv_id:
        for m in db.get_conversation_history(conv_id, u):
            with st.chat_message("user" if m["role"] == "user" else "assistant"):
                st.markdown(m["content"])
    q = st.chat_input("Ask about the records, for example: what changed since the last visit?")
    if q:
        if provider is None:
            st.error("No model key configured. Set OPENAI_API_KEY to run the board.")
            return
        if not conv_id:
            conv_id = db.create_conversation(u, pid, q[:60])
            st.session_state.conversation_id = conv_id
        db.save_chat_message(pid, conv_id, u, "user", q)
        if len(db.get_conversation_history(conv_id, u)) == 1:
            db.update_conversation_title(conv_id, u, q[:60])
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            with st.spinner("The board is reading the records..."):
                try:
                    run = board.run_board(pid, u, q, provider, language=lang, conversation_id=conv_id, force_all=force_all)
                except Exception as e:
                    _denied(e)
                    return
            md = board.format_markdown(run)
            st.markdown(md)
            if run.get("errors"):
                st.warning("; ".join(run["errors"]))
        db.save_chat_message(pid, conv_id, u, "assistant", md, responding_doctor="coordinator",
                             metadata={"run_id": run["id"], "cost_usd": run.get("estimated_cost_usd")})
        st.rerun()
    with st.expander("Past runs and cost"):
        runs = board.list_runs(pid, u)
        if runs:
            total = sum(r["estimated_cost_usd"] or 0 for r in runs)
            st.caption(f"{len(runs)} runs, estimated total ${total:.4f}")
            st.dataframe(pd.DataFrame([{"When": _fmt(r["created_at"]), "Question": r["query"][:60], "Specialists": ", ".join((r["routed_agents"] or {}).get("agents", [])),
                                        "Tokens in": r["input_tokens"], "Tokens out": r["output_tokens"], "Cost $": r["estimated_cost_usd"], "Status": r["status"]} for r in runs]),
                         hide_index=True, width="stretch")
        else:
            st.caption("No runs yet.")


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def page_brief() -> None:
    u = _user()
    pid = _need_patient()
    if not pid:
        return
    st.title("Pre-visit brief")
    rows = appts.list_appointments(pid, u, upcoming_only=True)
    opts = {0: "General (all open questions)"} | {a["id"]: f"{_fmt(a['starts_at'])} {a['title']}" for a in rows}
    chosen = st.selectbox("For which appointment?", list(opts), format_func=lambda i: opts[i])
    lang = st.radio("Language", list(LANGS), format_func=lambda k: LANGS[k], horizontal=True,
                    index=list(LANGS).index(u.get("language") or "en"))
    b = brief_mod.build_brief(pid, u, appointment_id=chosen or None)
    md = brief_mod.brief_markdown(b, lang)
    c1, c2 = st.columns(2)
    c1.download_button("Download PDF", brief_mod.brief_pdf(b, lang), file_name=f"brief-{b['patient']['name']}.pdf", mime="application/pdf")
    c2.download_button("Download text", md, file_name=f"brief-{b['patient']['name']}.md", mime="text/markdown")
    st.markdown(md)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def page_settings() -> None:
    u = _user()
    st.title("Settings")
    tabs = ["Account"] + (["People and invites", "Specialists", "Audit log"] if _can("admin") else [])
    t = st.tabs(tabs)
    with t[0]:
        with st.form("profile"):
            name = st.text_input("Your name", u.get("display_name") or "")
            lang = st.selectbox("Language for board output and briefs", list(LANGS), format_func=lambda k: LANGS[k],
                                index=list(LANGS).index(u.get("language") or "en"))
            if st.form_submit_button("Save"):
                db.update_user_settings(u, display_name=name, language=lang)
                _refresh_user()
                st.success("Saved.")
        with st.form("password"):
            st.markdown("**Change password**")
            cur = st.text_input("Current password", type="password")
            new = st.text_input("New password", type="password")
            if st.form_submit_button("Change"):
                try:
                    auth.change_password(u, cur, new)
                    st.success("Password changed.")
                except auth.AuthError as e:
                    st.error(str(e))
    if not _can("admin"):
        return
    with t[1]:
        st.markdown("**Invite someone**")
        c1, c2 = st.columns([1, 2])
        role = c1.selectbox("Role", ["viewer", "editor", "admin"], key="invite_role")
        if c1.button("Create invite code"):
            inv = auth.create_invite(u, role)
            st.session_state.last_invite = inv["code"]
        if st.session_state.get("last_invite"):
            c2.code(st.session_state.last_invite)
            c2.caption("Send this code. It works once and expires in 14 days.")
        invites = [i for i in auth.list_invites(u) if not i["used_at"]]
        if invites:
            st.dataframe(pd.DataFrame([{"Code": i["code"], "Role": i["role"], "Expires": _fmt(i["expires_at"])} for i in invites]),
                         hide_index=True, width="stretch")
        st.markdown("**People**")
        for person in db.list_users():
            if not person.get("email"):
                continue
            c1, c2, c3, c4 = st.columns([3, 2, 1, 1])
            c1.write(f"{person['display_name']}  \n{person['email']}")
            new_role = c2.selectbox("Role", ["viewer", "editor", "admin"], index=["viewer", "editor", "admin"].index(person["role"]),
                                    key=f"role_{person['id']}", label_visibility="collapsed")
            if new_role != person["role"]:
                db.set_user_role(u, person["id"], new_role)
                st.rerun()
            active = c3.toggle("Active", value=bool(person["is_active"]), key=f"active_{person['id']}", disabled=person["id"] == u["id"])
            if active != bool(person["is_active"]):
                db.set_user_active(u, person["id"], active)
                st.rerun()
            if c4.button("Reset password", key=f"reset_{person['id']}"):
                st.info(f"Temporary password for {person['display_name']}: `{auth.reset_password(u, person['id'])}`")
    with t[2]:
        st.caption("Switch specialists on or off. Every active specialist costs tokens on a full review. Edit prompts only if you know what you are doing; they must never ask for advice.")
        for a in db.get_doctor_agents(active_only=False):
            with st.expander(f"{a['display_name']} ({'on' if a['is_active'] else 'off'})"):
                on = st.toggle("Active", value=bool(a["is_active"]), key=f"agent_on_{a['agent_type']}")
                prompt = st.text_area("System prompt", a.get("system_prompt") or "", height=160, key=f"agent_prompt_{a['agent_type']}")
                model = st.text_input("Model (blank for default)", a.get("model") or "", key=f"agent_model_{a['agent_type']}")
                if st.button("Save", key=f"agent_save_{a['agent_type']}"):
                    db.update_doctor_agent(u, a["agent_type"], is_active=on, system_prompt=prompt, model=model or None)
                    st.success("Saved.")
                    st.rerun()
    with t[3]:
        log = db.get_audit_log(limit=300)
        if log:
            st.dataframe(pd.DataFrame([{"When": _fmt(r["created_at"]), "Who": r.get("display_name") or r.get("user_id"), "Action": r["action"],
                                        "Patient": r.get("patient_id"), "Details": str(r.get("details") or "")[:120]} for r in log]),
                         hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title=APP_TITLE, page_icon=":hospital:", layout="wide")
    if not os.environ.get("DATABASE_URL"):
        st.error("DATABASE_URL is not set.")
        return
    try:
        _startup()
    except Exception as e:
        st.error(f"Database migration failed: {e}")
        return
    if not _user():
        render_login()
        return
    _refresh_user()
    if not _user():
        render_login()
        return
    render_sidebar()
    pages = [
        st.Page(page_home, title="Home", icon=":material/home:", default=True),
        st.Page(page_documents, title="Documents", icon=":material/upload_file:"),
        st.Page(page_medications, title="Medications", icon=":material/medication:"),
        st.Page(page_values, title="Values", icon=":material/monitoring:"),
        st.Page(page_appointments, title="Appointments", icon=":material/calendar_month:"),
        st.Page(page_board, title="Board", icon=":material/groups:"),
        st.Page(page_brief, title="Brief", icon=":material/description:"),
        st.Page(page_settings, title="Settings", icon=":material/settings:"),
    ]
    st.navigation(pages).run()


main()
