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
import streamlit.components.v1 as components

import appointments as appts
import auth
import board
import brief as brief_mod
import consultations
import database as db
import documents
import medications as meds
import ui
import values_import
from extraction import parse_date, rows_from_vital_payload as extraction_rows
from providers import ProviderError, get_provider

APP_TITLE = "Family Medical Board"
DISCLAIMER = "This app keeps records and lists questions for the treating doctors. It never gives medical advice."
KINDS = {"auto": "Detect automatically", "prescription": "Prescription", "lab_report": "Lab report",
         "imaging_report": "Imaging report", "vitals_log": "Readings log (BP, sugar, weight)",
         "discharge_summary": "Discharge summary or letter", "consultation_audio": "Doctor conversation (audio)",
         "other": "Other"}
LANGS = {"en": "English", "ur": "Urdu"}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@st.cache_resource(show_spinner="Preparing the database...")
def _startup() -> dict:
    return db.run_migrations()


@st.cache_resource
def _install_home_screen_support() -> str:
    """Patch Streamlit's index.html once per process so iOS and Android can add
    the app to the home screen (manifest, apple-touch-icon, standalone mode).
    Streamlit serves that file from disk on every request, so the patch is live
    from the next page load. Failures are logged, never shown."""
    import streamlit as _st
    marker = "<!-- fmb-home-screen -->"
    tags = (
        f"{marker}\n"
        '    <link rel="manifest" href="./app/static/manifest.json" />\n'
        '    <link rel="apple-touch-icon" href="./app/static/icon-180.png" />\n'
        '    <meta name="apple-mobile-web-app-capable" content="yes" />\n'
        '    <meta name="mobile-web-app-capable" content="yes" />\n'
        '    <meta name="apple-mobile-web-app-status-bar-style" content="default" />\n'
        f'    <meta name="apple-mobile-web-app-title" content="{APP_TITLE}" />\n'
        '    <meta name="theme-color" content="#ffffff" />\n'
    )
    try:
        path = os.path.join(os.path.dirname(_st.__file__), "static", "index.html")
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        if marker in html:
            return "already patched"
        patched = html.replace("<title>Streamlit</title>", tags + f"    <title>{APP_TITLE}</title>", 1)
        if patched == html:
            return "title tag not found, skipped"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(patched)
        return "patched"
    except Exception as e:  # read-only install or unexpected layout
        return f"skipped: {e}"


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
            st.session_state.user = {k: fresh.get(k) for k in ("id", "email", "display_name", "role", "language", "is_active", "last_patient_id")}
        else:
            st.session_state.pop("user", None)


def _tz() -> str | None:
    try:
        return st.context.timezone
    except Exception:
        return None


def _when(value) -> str:
    return appts.format_local(value, _tz())


def _remember(user: dict) -> None:
    """Sign-in succeeded: keep it across reloads with a cookie backed by a sessions row."""
    st.session_state.user = user
    try:
        token = auth.create_session(user)
    except Exception:
        return
    st.session_state.session_token = token
    st.session_state.cookie_action = ("set", token)


def _cookie_js() -> None:
    """Emit the pending cookie change through a zero-height component. The
    component iframe is same-origin, so it may write the parent's cookies."""
    action = st.session_state.pop("cookie_action", None)
    if not action:
        return
    kind, token = action
    if kind == "set":
        value = f"{auth.SESSION_COOKIE}={token}; Path=/; Max-Age={auth.SESSION_DAYS * 86400}; SameSite=Lax"
    else:
        value = f"{auth.SESSION_COOKIE}=; Path=/; Max-Age=0; SameSite=Lax"
    components.html(
        "<script>(function(){var d=window.parent.document;var v=" + repr(value) + ";"
        "if(window.parent.location.protocol==='https:'){v+='; Secure';}d.cookie=v;})();</script>",
        height=0)


def _restore_from_cookie() -> None:
    """A reload or a new Streamlit session: pick the user up from the cookie."""
    if st.session_state.get("signed_out"):
        return
    try:
        token = st.context.cookies.get(auth.SESSION_COOKIE)
    except Exception:
        token = None
    if not token:
        return
    try:
        user = auth.user_from_session(token)
    except Exception:
        user = None
    if user:
        st.session_state.user = user
        st.session_state.session_token = token
        if user.get("last_patient_id"):
            st.session_state.current_patient_id = user["last_patient_id"]


def _can(role: str) -> bool:
    u = _user()
    return bool(u) and db.ROLE_RANK.get(u.get("role", "viewer"), 0) >= db.ROLE_RANK[role]


def _denied(e: Exception) -> None:
    st.error("You do not have access to do that." if isinstance(e, PermissionError) else str(e))


def _guess_base_url() -> str:
    """Public URL for invite links: APP_BASE_URL, else the Replit deployment host, else the request host."""
    for key in ("REPLIT_DEPLOYMENT_URL", "REPLIT_DEV_DOMAIN"):
        v = os.environ.get(key)
        if v:
            return v if v.startswith("http") else f"https://{v}"
    try:
        host = st.context.headers.get("host")
        if host:
            return f"https://{host}"
    except Exception:
        pass
    return ""


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
    st.markdown("One calm place for a family to keep a parent's medical record, prepare for visits, and stay in step with the doctors.")
    st.caption(DISCLAIMER)
    first = auth.is_first_user()
    invite_param = (st.query_params.get("invite") or "").strip()
    if invite_param and "signup_code" not in st.session_state:
        st.session_state.signup_code = invite_param
    if invite_param:
        st.success("You have an invite. Open the Create account tab, enter your name and email, and choose a password.")
    tab_in, tab_up, tab_forgot = st.tabs(["Sign in", "Create account", "Forgot password"])
    with tab_in:
        with st.form("login"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            if st.form_submit_button("Sign in", type="primary", width="stretch"):
                try:
                    _remember(auth.login(email, password))
                    st.rerun()
                except auth.AuthError as e:
                    st.error(str(e))
    with tab_up:
        if first:
            st.info("No accounts exist yet. The first account becomes the admin.")
        elif not invite_param:
            st.info("Accounts are by invitation only. Ask the family admin for an invite link or code.")
        with st.form("signup"):
            name = st.text_input("Your name", key="signup_name")
            email = st.text_input("Email", key="signup_email")
            password = st.text_input("Password (at least 10 characters)", type="password", key="signup_password")
            code = "" if first else st.text_input("Invite code", key="signup_code")
            if st.form_submit_button("Create account", type="primary", width="stretch"):
                try:
                    _remember(auth.signup(email, password, name, code or None))
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
                    _remember(auth.reset_password_with_secret(email, code, password))
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
        current = st.session_state.get("current_patient_id") or u.get("last_patient_id")
        if current not in options:
            current = next(iter(options), None)
        if options:
            chosen = st.selectbox("Patient", list(options), index=list(options).index(current) if current else 0,
                                  format_func=lambda i: options[i], key="patient_select")
            st.session_state.current_patient_id = chosen
            if chosen != u.get("last_patient_id"):
                try:
                    db.set_last_patient(u, chosen)
                    st.session_state.user["last_patient_id"] = chosen
                except Exception:
                    pass
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
            try:
                auth.end_session(st.session_state.get("session_token"))
            except Exception:
                pass
            for k in ("user", "current_patient_id", "conversation_id", "session_token"):
                st.session_state.pop(k, None)
            st.session_state.signed_out = True
            st.session_state.cookie_action = ("clear", None)
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

def _greeting() -> str:
    try:
        from zoneinfo import ZoneInfo
        hour = datetime.now(ZoneInfo(_tz() or "UTC")).hour
    except Exception:
        hour = datetime.now().hour
    return "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")


def page_home() -> None:
    u = _user()
    first_name = (u.get("display_name") or "").split(" ")[0]
    st.title(f"{_greeting()}, {first_name}" if first_name else _greeting())
    due = appts.due_reminders(u)
    if due:
        for a in due:
            st.warning(f"Coming up: {a['patient_name']}, {a['title']} on {_when(a["starts_at"])}"
                       + (f" with {a['doctor_name']}" if a.get("doctor_name") else ""))
    pid = _need_patient()
    if not pid:
        st.caption("Start by adding the person you care for, in the side panel.")
        return
    patient = db.get_patient(pid, u)
    pending = sum(len(v) for v in documents.pending_review(pid, u).values())
    active = meds.list_medications(pid, u, status="active")
    docs = db.list_documents(pid, u)
    upcoming = appts.list_appointments(pid, u, upcoming_only=True, limit=5)
    st.subheader(f"Looking after {patient['name']}")
    if pending:
        st.info(f"{pending} things were read from documents and are waiting for a quick check. Nothing is saved until you confirm it. "
                "Open Documents when you have a minute.")
    elif not docs and not active:
        st.success("A clean start. Add a prescription photo on Medications, or a lab report on Documents, and the record builds itself.")
    else:
        st.success("Everything is in order. Nothing is waiting for you.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("To check", pending)
    c2.metric("Medicines in use", len(active))
    c3.metric("Documents kept", len(docs))
    c4.metric("Visits ahead", len(upcoming))
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
                st.write(f"{_when(a['starts_at'])}: {a['title']}" + (f" with {a['doctor_name']}" if a.get("doctor_name") else ""))
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
        c1, c2 = st.columns(2)
        if c1.button("Archive this patient", key="archive_patient", help="Hides the patient; records are kept."):
            db.archive_patient(patient["id"], u)
            st.session_state.current_patient_id = None
            st.rerun()
        confirm = c2.text_input("Type DELETE to remove this patient and every record permanently", key="delete_patient_confirm")
        if c2.button("Delete patient permanently", key="delete_patient", type="secondary", disabled=confirm != "DELETE"):
            try:
                db.delete_patient(patient["id"], u)
                st.session_state.current_patient_id = None
                st.success("Patient deleted.")
                st.rerun()
            except Exception as e:
                _denied(e)


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
    _review_items(pid, grouped)


def _review_items(pid: int, grouped: dict) -> None:
    u = _user()
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
                    elif doc.get("document_kind") == "consultation_audio":
                        st.caption("From a doctor conversation. Listen again if a field looks wrong.")
                        full = db.get_document(file_id, pid, u, with_data=True)
                        if full and full.get("file_data"):
                            st.audio(bytes(full["file_data"]), format=full.get("mime_type") or "audio/mpeg")
                        if p_change := it["payload"].get("change"):
                            st.caption(f"Said as: {p_change}" + (f", reason: {it['payload']['reason']}" if it["payload"].get("reason") else ""))
                    else:
                        st.caption("No page image.")
                with right:
                    _item_editor(it, pid, editable)
                st.markdown("---")


def _confirmable(item: dict) -> bool:
    if item["item_type"] == "lab_value":
        return item["payload"].get("numeric_value") is not None
    if item["item_type"] == "vital":
        return bool(extraction_rows(item["payload"]))
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
        elif it["item_type"] == "vital":
            kinds = ["blood_pressure", "glucose", "weight", "heart_rate", "temperature", "oxygen_saturation", "other"]
            new["kind"] = st.selectbox("Kind", kinds, index=kinds.index(p.get("kind") or "other"), key=f"{key}_kind")
            if (p.get("kind") or "") == "blood_pressure":
                c1, c2, c3 = st.columns(3)
                new["systolic"] = c1.number_input("Systolic", value=float(p.get("systolic") or 0), key=f"{key}_sys")
                new["diastolic"] = c2.number_input("Diastolic", value=float(p.get("diastolic") or 0), key=f"{key}_dia")
                new["pulse"] = c3.number_input("Pulse", value=float(p.get("pulse") or 0), key=f"{key}_pulse") or None
            else:
                new["name"] = st.text_input("Name (for other)", p.get("name") or "", key=f"{key}_name") or None
                new["value"] = st.number_input("Value" + (" (unsure)" if "value" in low else ""), value=float(p.get("value") or 0), format="%.2f", key=f"{key}_val")
                new["unit"] = st.text_input("Unit", p.get("unit") or "", key=f"{key}_unit") or None
            new["recorded_at"] = st.text_input("Date and time as written" + (" (unsure)" if "recorded_at" in low else ""), p.get("recorded_at") or "", key=f"{key}_when") or None
            new["notes"] = st.text_input("Notes", p.get("notes") or "", key=f"{key}_notes") or None
        elif it["item_type"] == "appointment":
            new["title"] = st.text_input("Title", p.get("title") or "", key=f"{key}_title")
            new["starts_at"] = st.text_input("Date (YYYY-MM-DD, add THH:MM for a time)", p.get("starts_at") or "", key=f"{key}_when")
            new["doctor_name"] = st.text_input("Doctor", p.get("doctor_name") or "", key=f"{key}_doc") or None
            new["specialty"] = st.text_input("Specialty", p.get("specialty") or "", key=f"{key}_spec") or None
            new["notes"] = st.text_input("As said", p.get("notes") or "", key=f"{key}_notes") or None
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
    provider = _provider()
    if _can("editor"):
        with st.expander("Add from a prescription photo or PDF", expanded=not meds.list_medications(pid, u, status="active")):
            st.caption("Upload the prescription. The medicines are read out for you to check and confirm; nothing is saved until you confirm.")
            c1, c2 = st.columns(2)
            files = c1.file_uploader("Prescription files", type=["pdf", "jpg", "jpeg", "png", "webp"], accept_multiple_files=True, key="rx_uploader")
            shot = c2.camera_input("Or take a photo of it", key="rx_camera")
            uploads = list(files or []) + ([shot] if shot is not None else [])
            if uploads and st.button("Read the prescription", type="primary", key="rx_upload_btn"):
                for f in uploads:
                    name = getattr(f, "name", None) or f"prescription-{datetime.now():%Y%m%d-%H%M%S}.jpg"
                    with st.spinner(f"Reading {name}..."):
                        try:
                            out = documents.ingest_upload(pid, u, name, getattr(f, "type", None) or "application/octet-stream",
                                                          f.getvalue(), provider, kind="prescription")
                        except Exception as e:
                            _denied(e)
                            continue
                    if out.get("error"):
                        st.error(f"{name}: stored, but reading it failed: {out['error']}")
                    elif provider is None:
                        st.warning(f"{name}: stored, but no model key is configured so nothing was read.")
                    else:
                        st.success(f"{name}: {out['items']} items found. Check them below.")
                st.rerun()
    pending_meds = {fid: [i for i in items if i["item_type"] in ("medication", "diagnosis")]
                    for fid, items in documents.pending_review(pid, u).items()}
    pending_meds = {fid: items for fid, items in pending_meds.items() if items}
    if pending_meds:
        st.subheader("Check these before they join the list")
        _review_items(pid, pending_meds)
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
    pending_vitals = {fid: [i for i in items if i["item_type"] == "vital"] for fid, items in documents.pending_review(pid, u).items()}
    pending_vitals = {fid: items for fid, items in pending_vitals.items() if items}
    if pending_vitals:
        st.subheader("Check these readings before they are saved")
        _review_items(pid, pending_vitals)
    if _can("editor"):
        provider = _provider()
        tab_file, tab_text, tab_photo, tab_manual = st.tabs(["Import a spreadsheet", "Paste or type readings", "Photo of a log", "One reading by hand"])
        with tab_file:
            st.caption("CSV or Excel. Columns such as date, type, value, unit, or systolic, diastolic, pulse, or one column per reading (glucose, weight, SpO2) are recognised. You can correct the preview before adding.")
            up = st.file_uploader("Spreadsheet", type=["csv", "xlsx", "xls", "tsv", "txt"], key="values_file")
            if up is not None:
                try:
                    rows = values_import.parse_table(up.getvalue(), up.name)
                except Exception as e:
                    rows = []
                    st.error(f"Could not read the file: {e}")
                if rows:
                    prev = pd.DataFrame(rows)[["recorded_at", "reading_type", "value", "unit", "notes", "source_row"]]
                    edited = st.data_editor(prev, hide_index=True, width="stretch", num_rows="dynamic", key="values_preview")
                    if st.button(f"Add {len(edited)} readings", type="primary", key="values_add_btn"):
                        try:
                            approved = edited.to_dict("records")
                            for r in approved:
                                r["recorded_at"] = values_import._when(r.get("recorded_at"))
                            n = values_import.add_readings(pid, u, approved, source=f"Import: {up.name}")
                            st.success(f"Added {n} readings.")
                            st.rerun()
                        except Exception as e:
                            _denied(e)
                elif up is not None:
                    st.warning("No readings recognised in that file. Check the column names.")
        with tab_text:
            st.caption("Type or paste notes such as: BP 140/90 pulse 72 on 1 Oct morning; sugar 150 fasting 2 Oct; weight 71 kg 3 Oct. They are read out for you to check before saving.")
            text = st.text_area("Readings", key="values_text", height=120)
            if st.button("Read these notes", key="values_text_btn", disabled=not text.strip()):
                if provider is None:
                    st.error("No model key configured. Use the spreadsheet import or manual entry.")
                else:
                    with st.spinner("Reading..."):
                        try:
                            out = documents.stage_text_readings(pid, u, text.strip(), provider)
                            st.success(f"{out['items']} readings found. Check them above and confirm.")
                            st.rerun()
                        except Exception as e:
                            _denied(e)
        with tab_photo:
            st.caption("A photo of a BP diary, glucose meter screen or logbook, or a weight chart. Readings are extracted for you to confirm.")
            shot = st.file_uploader("Photo or PDF of the log", type=["pdf", "jpg", "jpeg", "png", "webp"], key="values_photo")
            cam = st.camera_input("Or take the photo now", key="values_camera")
            f = shot or cam
            if f is not None and st.button("Read the log", key="values_photo_btn"):
                name = getattr(f, "name", None) or f"readings-{datetime.now():%Y%m%d-%H%M%S}.jpg"
                with st.spinner("Reading..."):
                    try:
                        out = documents.ingest_upload(pid, u, name, getattr(f, "type", None) or "application/octet-stream", f.getvalue(), provider, kind="vitals_log")
                    except Exception as e:
                        out = {"error": str(e)}
                if out.get("error"):
                    st.error(out["error"])
                else:
                    st.success(f"{out['items']} readings found. Check them above and confirm.")
                    st.rerun()
        with tab_manual, st.form("add_value", clear_on_submit=True):
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
    tz = _tz()
    st.caption(f"Times are shown in your device's timezone ({tz or 'UTC'}).")
    patient = db.get_patient(pid, u) or {}
    rows = appts.list_appointments(pid, u)
    now_utc = datetime.utcnow()
    upcoming = [a for a in rows if a["status"] == "scheduled" and a["starts_at"] >= now_utc - timedelta(hours=1)]
    past = [a for a in rows if a not in upcoming]
    st.subheader("Upcoming")
    if upcoming:
        for a in upcoming:
            a = dict(a, patient_name=patient.get("name"))
            head = f"{_when(a['starts_at'])}: {a['title']}" + (f" with {a['doctor_name']}" if a.get("doctor_name") else "")
            with st.expander(head, expanded=False):
                if a.get("specialty") or a.get("location"):
                    st.write(", ".join(x for x in (a.get("specialty"), a.get("location")) if x))
                if a.get("notes"):
                    st.caption(a["notes"])
                st.markdown("**Add to a calendar**")
                g, o, i = st.columns(3)
                g.link_button("Google Calendar", appts.google_calendar_url(a), width="stretch")
                o.link_button("Outlook", appts.outlook_calendar_url(a), width="stretch")
                i.download_button("Apple or other (.ics)", appts.to_ics([a]), file_name=f"appointment-{a['id']}.ics",
                                  mime="text/calendar", key=f"ics_{a['id']}", width="stretch")
                w, d, c = st.columns(3)
                w.link_button("Send on WhatsApp", appts.whatsapp_share_url(appts.share_text(a, tz)), width="stretch")
                if _can("editor"):
                    if d.button("Mark done", key=f"done_{a['id']}", width="stretch"):
                        appts.update_appointment(a["id"], pid, u, status="completed")
                        st.rerun()
                    if c.button("Cancel it", key=f"cancel_{a['id']}", width="stretch"):
                        appts.update_appointment(a["id"], pid, u, status="cancelled")
                        st.rerun()
    else:
        st.caption("Nothing scheduled.")
    if upcoming:
        st.download_button("Download all upcoming (.ics)", appts.to_ics([dict(a, patient_name=patient.get("name")) for a in upcoming]),
                           file_name="appointments.ics", mime="text/calendar")
        st.caption("On an iPhone, open the .ics file and tap Add to Calendar. Google and Outlook open pre-filled in the browser or app.")
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
                    appts.add_appointment(pid, u, title.strip(), appts.from_local(day, tm, tz), kind=kind, doctor_name=doctor or None,
                                          specialty=spec or None, location=loc or None, reminder_minutes=int(rem), notes=notes or None)
                    st.rerun()
                except Exception as e:
                    _denied(e)
    if past:
        with st.expander("Past and cancelled"):
            st.dataframe(pd.DataFrame([{"When": _when(a["starts_at"]), "Title": a["title"], "Doctor": a.get("doctor_name") or "", "Status": a["status"]} for a in past]),
                         hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Doctor conversations (audio)
# ---------------------------------------------------------------------------

def page_conversations() -> None:
    u = _user()
    pid = _need_patient()
    if not pid:
        return
    st.title("Doctor conversations")
    st.caption("Record or upload the audio of a visit. The app writes down what was said, works out who the doctor was, "
               "and lists the actions and medication changes that were spoken. It adds nothing of its own.")
    provider = _provider()
    if _can("editor"):
        tab_rec, tab_up = st.tabs(["Record now", "Upload a recording"])
        with tab_rec:
            st.caption("Ask the doctor first. Hold the phone near the people speaking.")
            clip = st.audio_input("Record the conversation", key="conv_record")
            visit_day = st.date_input("Date of the visit", date.today(), key="conv_rec_date")
            lang = st.selectbox("Spoken language", ["auto", "en", "ur"], format_func=lambda x: {"auto": "Detect", "en": "English", "ur": "Urdu"}[x], key="conv_rec_lang")
            if clip is not None and st.button("Transcribe and summarize", type="primary", key="conv_rec_go"):
                _ingest_conversation(pid, u, clip, provider, visit_day, lang)
        with tab_up:
            up = st.file_uploader("Audio file (m4a, mp3, wav, voice note)", type=["m4a", "mp3", "wav", "webm", "ogg", "aac", "flac", "mp4"], key="conv_upload")
            visit_day2 = st.date_input("Date of the visit", date.today(), key="conv_up_date")
            lang2 = st.selectbox("Spoken language", ["auto", "en", "ur"], format_func=lambda x: {"auto": "Detect", "en": "English", "ur": "Urdu"}[x], key="conv_up_lang")
            if up is not None and st.button("Transcribe and summarize", type="primary", key="conv_up_go"):
                _ingest_conversation(pid, u, up, provider, visit_day2, lang2)
    rows = consultations.list_consultations(pid, u)
    pending = {fid: items for fid, items in documents.pending_review(pid, u).items()
               if any(i["item_type"] in ("medication", "appointment") for i in items)}
    conv_files = {c["file_id"] for c in rows if c.get("file_id")}
    pending = {fid: items for fid, items in pending.items() if fid in conv_files}
    if pending:
        st.subheader("Said in a conversation, waiting for your confirmation")
        st.caption("Medications and follow-ups only enter the record after you confirm them here.")
        _review_items(pid, pending)
    st.subheader("Conversations on record")
    if not rows:
        st.caption("Nothing recorded yet.")
    for c in rows:
        label = f"{c['happened_on'] or ''} {c['title']}" + (f" ({c['doctor_name']})" if c.get("doctor_name") else "")
        with st.expander(label.strip(), expanded=False):
            _conversation_detail(c, pid, u, provider)


def _ingest_conversation(pid: int, u: dict, f, provider, visit_day, lang: str) -> None:
    data = f.getvalue()
    name = getattr(f, "name", None) or f"recording-{datetime.now():%Y%m%d-%H%M%S}.wav"
    mime = getattr(f, "type", None) or "audio/wav"
    with st.spinner("Transcribing and summarizing. A 20 minute visit takes about a minute."):
        try:
            out = consultations.ingest_audio(pid, u, name, mime, data, provider, language=None if lang == "auto" else lang,
                                             happened_on=visit_day)
        except Exception as e:
            _denied(e)
            return
    if out.get("error"):
        st.error(f"Stored, but not processed: {out['error']}")
    else:
        st.success(f"Done. {out['items']} medication or follow-up items are waiting for your confirmation below.")
    st.rerun()


def _conversation_detail(c: dict, pid: int, u: dict, provider) -> None:
    s = c.get("summary") if isinstance(c.get("summary"), dict) else None
    if c["status"] == "error":
        st.error(f"Not processed: {c.get('error')}")
        if _can("editor") and provider is not None and st.button("Try again", key=f"retry_{c['id']}"):
            with st.spinner("Processing..."):
                consultations.process(c["id"], pid, u, provider, patient_name=(db.get_patient(pid, u) or {}).get("name"))
            st.rerun()
    elif c["status"] == "processing":
        st.info("Still processing.")
    if s:
        conf = s.get("doctor_confidence") or 0
        st.markdown(f"**Doctor:** {s.get('doctor_name') or 'not identified'}"
                    + (f", {s['doctor_specialty']}" if s.get("doctor_specialty") else "") + f" (confidence {conf:.0%})")
        st.markdown(f"**Context:** {s.get('context', '')}")
        st.markdown(f"**Summary:** {s.get('summary', '')}")
        if s.get("decisions"):
            st.markdown("**Decisions**")
            for d in s["decisions"]:
                st.markdown(f"- {d}")
        if s.get("actions"):
            st.markdown("**Actions asked for**")
            for a in s["actions"]:
                st.markdown(f"- {a['text']}" + (f" ({a['who']}, {a['when']})" if a.get("when") else f" ({a['who']})"))
        if s.get("medication_changes"):
            st.markdown("**Medication changes said out loud**")
            st.dataframe(pd.DataFrame([{"Medicine": m["name"], "Dose": m.get("dose") or "", "Frequency": m.get("frequency") or "",
                                        "Change": m["change"], "Reason": m.get("reason") or "", "Confidence": f"{m['confidence']:.0%}"}
                                       for m in s["medication_changes"]]), hide_index=True, width="stretch")
        if s.get("follow_ups"):
            st.markdown("**Follow-ups**")
            for f in s["follow_ups"]:
                st.markdown(f"- {f['title']}" + (f", {f['when_text']}" if f.get("when_text") else "") + (f" ({f['date']})" if f.get("date") else ""))
        if s.get("questions_left_open"):
            st.markdown("**Raised but not answered**")
            for q in s["questions_left_open"]:
                st.markdown(f"- {q}")
        if s.get("findings_discussed"):
            with st.expander("Findings mentioned"):
                for x in s["findings_discussed"]:
                    st.markdown(f"- {x}")
        with st.expander("Transcript"):
            roles = {sp["label"]: sp for sp in s.get("speakers", [])}
            for t in s.get("turns", []):
                sp = roles.get(t["speaker"], {})
                who = sp.get("name") or f"{t['speaker']} ({sp.get('role', 'unknown')})"
                st.markdown(f"**{who}:** {t['text']}")
    doc = db.get_document(c["file_id"], pid, u, with_data=True) if c.get("file_id") else None
    if doc and doc.get("file_data"):
        st.audio(bytes(doc["file_data"]), format=doc.get("mime_type") or "audio/mpeg")
    meta = []
    if c.get("duration_seconds"):
        meta.append(f"{float(c['duration_seconds']) / 60:.1f} min")
    if c.get("cost_usd") is not None:
        meta.append(f"model cost ${float(c['cost_usd']):.3f}")
    if c.get("transcription_model"):
        meta.append(c["transcription_model"])
    if meta:
        st.caption(", ".join(meta))
    if _can("editor"):
        with st.form(f"conv_edit_{c['id']}"):
            c1, c2 = st.columns(2)
            title = c1.text_input("Title", c["title"])
            when = c2.date_input("Date", c["happened_on"] or date.today())
            doctor = c1.text_input("Doctor", c.get("doctor_name") or "")
            spec = c2.text_input("Specialty", c.get("specialty") or "")
            b1, b2 = st.columns(2)
            if b1.form_submit_button("Save", width="stretch"):
                consultations.update_consultation(c["id"], pid, u, title=title.strip() or c["title"], happened_on=when,
                                                  doctor_name=doctor or None, specialty=spec or None)
                st.rerun()
            if b2.form_submit_button("Delete this conversation", width="stretch"):
                consultations.delete_consultation(c["id"], pid, u)
                st.rerun()


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
    with st.expander("Past runs, full workings and cost"):
        runs = board.list_runs(pid, u)
        if runs:
            total = sum(r["estimated_cost_usd"] or 0 for r in runs)
            st.caption(f"{len(runs)} runs, estimated total ${total:.4f}. Every run is kept: the question, who was asked, "
                       "what each specialist said, and what the coordinator wrote.")
            st.dataframe(pd.DataFrame([{"When": _when(r["created_at"]), "Question": r["query"][:60], "Specialists": ", ".join((r["routed_agents"] or {}).get("agents", [])),
                                        "Tokens in": r["input_tokens"], "Tokens out": r["output_tokens"], "Cost $": r["estimated_cost_usd"], "Status": r["status"]} for r in runs]),
                         hide_index=True, width="stretch")
            labels = {r["id"]: f"{_when(r['created_at'])}: {r['query'][:70]}" for r in runs}
            chosen = st.selectbox("Open a run", list(labels), format_func=lambda i: labels[i], key="run_pick")
            if chosen:
                _run_detail(board.get_run(chosen, pid, u))
        else:
            st.caption("No runs yet.")


def _run_detail(run: dict | None) -> None:
    if not run:
        return
    import json as _json

    def _j(v):
        return _json.loads(v) if isinstance(v, str) else (v or {})

    routed = _j(run.get("routed_agents"))
    st.markdown(f"**Question:** {run['query']}")
    st.caption(f"Routed to {', '.join(routed.get('agents', []))}. {routed.get('reason') or ''}")
    if run.get("error"):
        st.warning(run["error"])
    for o in _j(run.get("specialist_outputs")) or []:
        with st.container(border=True):
            st.markdown(f"**{o.get('agent_type', '').replace('_', ' ').title()}** (confidence {o.get('confidence', 0):.0%})")
            st.write(o.get("summary", ""))
            for ob in o.get("observations", []):
                st.markdown(f"- {ob.get('text')}")
            for q in o.get("questions_for_doctor", []):
                st.markdown(f"- Ask {q.get('for_doctor')}: {q.get('question')}")
            if o.get("missing_records"):
                st.caption("Missing: " + ", ".join(o["missing_records"]))
            if o.get("declined_advice_request"):
                st.caption("Declined an advice request.")
    co = _j(run.get("coordinator_output"))
    if co:
        with st.container(border=True):
            st.markdown(f"**Coordinator:** {co.get('headline', '')}")
            for x in co.get("what_changed", []):
                st.markdown(f"- {x}")
            for g in co.get("questions_by_doctor", []):
                st.markdown(f"**For {g.get('doctor')}**")
                for q in g.get("questions", []):
                    st.markdown(f"- {q}")
    calls = _j(run.get("model_calls")) or []
    if calls:
        st.dataframe(pd.DataFrame([{"Purpose": c.get("purpose"), "Model": c.get("model"), "In": c.get("input_tokens"),
                                    "Out": c.get("output_tokens"), "Cost $": c.get("cost_usd"), "ms": c.get("duration_ms"),
                                    "Error": c.get("error") or ""} for c in calls]), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# Memory: what each agent remembers, editable
# ---------------------------------------------------------------------------

def page_memory() -> None:
    u = _user()
    pid = _need_patient()
    if not pid:
        return
    st.title("What the specialists remember")
    st.caption("Each specialist keeps short notes about this patient from board runs, confirmed documents and doctor "
               "conversations. They are shown to the specialists on the next run. Fix or remove anything wrong here.")
    rows = db.list_doctor_memories(pid, u)
    agents = {a["agent_type"]: a for a in db.get_doctor_agents(active_only=False)}
    names = {k: (a.get("display_name") or a.get("name") or k.replace("_", " ").title()) for k, a in agents.items()}
    if not rows:
        st.caption("Nothing remembered yet.")
        return
    by_agent: dict[str, list[dict]] = {}
    for r in rows:
        by_agent.setdefault(r["doctor_type"], []).append(r)
    editable = _can("editor")
    pick = st.selectbox("Specialist", ["all"] + list(by_agent), format_func=lambda k: "All specialists" if k == "all" else names.get(k, k), key="mem_pick")
    if _can("admin"):
        c1, c2 = st.columns([3, 1])
        c1.caption(f"{len(rows)} notes on record.")
        if c2.button("Clear " + ("all" if pick == "all" else names.get(pick, pick)), key="mem_clear"):
            st.session_state.mem_clear_confirm = True
        if st.session_state.get("mem_clear_confirm"):
            st.warning("This removes the notes for good. The documents and runs they came from stay.")
            if st.button("Yes, clear them", type="primary", key="mem_clear_yes"):
                db.clear_doctor_memories(pid, u, None if pick == "all" else pick)
                st.session_state.pop("mem_clear_confirm", None)
                st.rerun()
    for agent_type, items in by_agent.items():
        if pick != "all" and agent_type != pick:
            continue
        st.subheader(names.get(agent_type, agent_type))
        for m in items:
            src = []
            if m.get("source_run_id"):
                src.append(f"board run {m['source_run_id']}")
            if m.get("source_consultation_id"):
                src.append(f"conversation {m['source_consultation_id']}")
            if m.get("source_file_id"):
                src.append(f"document {m['source_file_id']}")
            if m.get("created_by_name"):
                src.append(f"by {m['created_by_name']}")
            with st.container(border=True):
                st.caption(f"{_when(m['created_at'])}, {m['memory_type'].replace('_', ' ')}" + (", from " + ", ".join(src) if src else "")
                           + (", edited" if m.get("updated_at") else ""))
                if editable:
                    with st.form(f"mem_{m['id']}"):
                        text = st.text_area("Note", m["content"], key=f"mem_text_{m['id']}", label_visibility="collapsed")
                        imp = st.slider("Importance", 1, 10, int(m.get("importance") or 5), key=f"mem_imp_{m['id']}")
                        b1, b2 = st.columns(2)
                        if b1.form_submit_button("Save", width="stretch"):
                            try:
                                db.update_doctor_memory(m["id"], pid, u, text, imp)
                                st.rerun()
                            except Exception as e:
                                _denied(e)
                        if b2.form_submit_button("Delete", width="stretch"):
                            db.delete_doctor_memory(m["id"], pid, u)
                            st.rerun()
                else:
                    st.write(m["content"])


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
    opts = {0: "General (all open questions)"} | {a["id"]: f"{_when(a["starts_at"])} {a['title']}" for a in rows}
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
        st.caption("Accounts are by invitation only. Each invite works once. Tie it to an email so nobody else can use it.")
        with st.form("invite_form"):
            c1, c2, c3 = st.columns([2, 1, 1])
            inv_email = c1.text_input("Their email (recommended)")
            inv_role = c2.selectbox("Role", ["viewer", "editor", "admin"], index=1)
            inv_note = c3.text_input("Who is this for?", placeholder="e.g. Sister")
            if st.form_submit_button("Create invite", type="primary"):
                try:
                    inv = auth.create_invite(u, inv_role, email=inv_email or None, note=inv_note or None)
                    st.session_state.last_invite = inv
                except Exception as e:
                    _denied(e)
        base_url = os.environ.get("APP_BASE_URL") or _guess_base_url()
        last = st.session_state.get("last_invite")
        if last:
            link = auth.invite_link(base_url, last["code"])
            msg = auth.invite_message(base_url, last)
            st.success("Invite created. Send it one of these ways:")
            st.code(link)
            from urllib.parse import quote
            w1, w2 = st.columns(2)
            w1.link_button("Send on WhatsApp", f"https://wa.me/?text={quote(msg)}", width="stretch")
            w2.link_button("Send by email", f"mailto:{last.get('email') or ''}?subject={quote('Invite to ' + APP_TITLE)}&body={quote(msg)}", width="stretch")
            st.caption(f"Or share the code {last['code']} by any other means.")
        invites = [i for i in auth.list_invites(u) if not i["used_at"]]
        if invites:
            st.markdown("**Open invites**")
            for i in invites:
                c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
                c1.write(i.get("email") or "any email")
                c2.write(f"{i['role']}" + (f", {i['note']}" if i.get("note") else ""))
                c3.write(f"expires {_fmt(i['expires_at'])[:10]}")
                if c4.button("Revoke", key=f"revoke_{i['id']}"):
                    auth.revoke_invite(u, i["id"])
                    st.rerun()
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
    ui.inject()
    if not os.environ.get("DATABASE_URL"):
        st.error("DATABASE_URL is not set.")
        return
    try:
        _startup()
    except Exception as e:
        st.error(f"Database migration failed: {e}")
        return
    _install_home_screen_support()
    _cookie_js()
    if not _user():
        _restore_from_cookie()
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
        st.Page(page_conversations, title="Conversations", icon=":material/mic:"),
        st.Page(page_board, title="Board", icon=":material/groups:"),
        st.Page(page_brief, title="Brief", icon=":material/description:"),
        st.Page(page_memory, title="Memory", icon=":material/psychology:"),
        st.Page(page_settings, title="Settings", icon=":material/settings:"),
    ]
    st.navigation(pages).run()


main()
