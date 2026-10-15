"""End-to-end smoke test with Playwright.

Run against a fresh database in demo mode:
  DATABASE_URL=... MODEL_PROVIDER=demo streamlit run app.py --server.port 8501 --server.headless true &
  python tests/e2e/smoke.py /tmp/shots
Not collected by pytest (no test_ prefix). Needs the playwright package and a Chromium
(set CHROMIUM_PATH if playwright did not install one).
"""
import os
import re
import sys
import time

import io
import struct
import wave

import pymupdf
from playwright.sync_api import expect, sync_playwright

BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8501")
SHOTS = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fmb-shots"
os.makedirs(SHOTS, exist_ok=True)


def pdf_bytes():
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 72), "Prescription: Metformin 500 mg twice daily. Dr. Demo")
    return doc.tobytes()


def wav_bytes(seconds=1.0, rate=8000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", 0) for _ in range(int(seconds * rate))))
    return buf.getvalue()


def shot(page, name):
    page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def wait_idle(page, ms=800):
    # Streamlit shows a "Running" status while a script runs.
    time.sleep(ms / 1000)
    try:
        page.wait_for_selector("[data-testid='stStatusWidget']", state="detached", timeout=30000)
    except Exception:
        pass
    time.sleep(0.3)


def nav(page, label):
    page.get_by_test_id("stSidebarNav").get_by_role("link", name=label).click()
    wait_idle(page)


with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(BASE)
    wait_idle(page, 2500)
    shot(page, "01-login")
    assert "Family Medical Board" in page.content()

    # First account becomes admin.
    page.get_by_role("tab", name="Create account").click()
    wait_idle(page)
    assert "first account becomes the admin" in page.content()
    page.get_by_label("Your name", exact=True).fill("Omer")
    page.get_by_test_id("stForm").filter(has_text="Create account").get_by_label("Email").fill("omer@example.com")
    page.get_by_label("Password (at least 10 characters)", exact=True).fill("correct horse battery")
    page.get_by_role("button", name="Create account").click()
    wait_idle(page, 2000)
    shot(page, "02-home-empty")
    assert "Add or select a patient" in page.content(), "home after signup"

    # Add a patient from the sidebar.
    page.get_by_text("Add patient").click()
    wait_idle(page)
    page.get_by_test_id("stSidebar").get_by_label("Name").fill("Parent One")
    page.get_by_test_id("stSidebar").get_by_role("button", name="Add").click()
    wait_idle(page, 1500)
    shot(page, "03-home-patient")
    assert "Parent One" in page.content()
    assert "Looking after Parent One" in page.inner_text("body")

    # Upload a prescription PDF in demo mode.
    nav(page, "Documents")
    page.get_by_test_id("stFileUploaderDropzoneInput").set_input_files({"name": "rx.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes()})
    wait_idle(page, 1500)
    page.get_by_role("button", name="Upload and extract").click()
    wait_idle(page, 4000)
    shot(page, "04-documents-review")
    text = page.inner_text("body")
    # The sure item was added on its own and the summary says so; the unsure one waits.
    assert "What was read" in text and "Added to the record" in text and "Metformin (demo)" in text, text[:1200]
    assert "Needs a look" in text and "Amlodipine (demo)" in text
    assert page.get_by_role("button", name="Reject").count() >= 1
    for _ in range(5):
        reject = page.get_by_role("button", name="Reject")
        if not reject.count():
            break
        reject.first.click()
        wait_idle(page, 2000)
    page.get_by_role("button", name="Dismiss").click()
    wait_idle(page, 1500)
    shot(page, "05-documents-done")
    text = page.inner_text("body")
    assert "Nothing waiting" in text and "What was read" not in text

    nav(page, "Medications")
    shot(page, "06-medications")
    assert "Metformin (demo)" in page.content()
    assert "History of changes" in page.content()

    nav(page, "Appointments")
    main = page.get_by_test_id("stMainBlockContainer")
    main.get_by_label("Title").fill("Diabetes follow-up")
    main.get_by_label("Doctor").fill("Dr. Khan")
    main.get_by_label("Specialty").fill("Endocrinology")
    main.get_by_role("button", name="Add").click()
    wait_idle(page, 2000)
    shot(page, "07-appointments")
    assert "Diabetes follow-up" in page.content()
    page.get_by_text("Diabetes follow-up with Dr. Khan").first.click()
    wait_idle(page, 800)
    hrefs = [a.get_attribute("href") for a in page.locator("a[href]").all()]
    assert any(h and h.startswith("https://calendar.google.com/calendar/render?action=TEMPLATE") for h in hrefs)
    assert any(h and h.startswith("https://outlook.live.com/calendar/") for h in hrefs)
    assert any(h and h.startswith("https://wa.me/?text=") for h in hrefs)
    assert page.get_by_role("button", name="Apple or other (.ics)").count() == 1
    shot(page, "07b-appointment-links")

    nav(page, "Conversations")
    page.get_by_role("tab", name="Upload a recording").click()
    wait_idle(page)
    page.locator("input[type='file']").last.set_input_files({"name": "visit.wav", "mimeType": "audio/wav", "buffer": wav_bytes()})
    wait_idle(page, 1500)
    page.get_by_role("button", name="Transcribe and summarize").last.click()
    wait_idle(page, 4000)
    shot(page, "07c-conversation")
    text = page.inner_text("body")
    assert "Diabetes review with Dr. Demo" in text and "Added to the record: Metformin (demo)" in text, text[:800]
    assert page.get_by_role("button", name="Confirm").count() == 0
    page.get_by_text("Diabetes review with Dr. Demo").first.click()
    wait_idle(page, 800)
    text = page.inner_text("body")
    assert "Repeat HbA1c in three months" in text and "Medication changes said out loud" in text and "Transcript" in text
    shot(page, "07d-conversation-detail")
    # A wrong upload can be removed: two taps, with a way back.
    page.get_by_role("button", name="Delete this conversation").first.click()
    wait_idle(page, 1000)
    assert "This cannot be undone" in page.inner_text("body")
    page.get_by_role("button", name="Keep it").click()
    wait_idle(page, 1000)
    assert "This cannot be undone" not in page.inner_text("body")
    assert "Diabetes review with Dr. Demo" in page.inner_text("body")

    nav(page, "Board")
    page.get_by_test_id("stChatInputTextArea").fill("What changed since the last visit?")
    page.keyboard.press("Enter")
    wait_idle(page, 5000)
    shot(page, "08-board")
    html = page.content()
    assert "Placeholder brief" in html or "demo" in html, "board answered"
    assert "Questions to ask" in html

    # Advice-seeking question is declined.
    page.get_by_test_id("stChatInputTextArea").fill("Should we increase his metformin dose?")
    page.keyboard.press("Enter")
    wait_idle(page, 5000)
    shot(page, "09-board-declined")
    assert "does not give medical advice" in page.content()

    nav(page, "Brief")
    shot(page, "10-brief")
    html = page.content()
    assert "Pre-visit brief" in html and "Metformin (demo)" in html
    assert page.get_by_role("button", name="Download PDF").count() == 1

    nav(page, "Values")
    shot(page, "11-values")
    nav(page, "Memory")
    wait_idle(page, 1000)
    text = page.inner_text("body")
    assert "What the specialists remember" in text and "board run" in text and "conversation" in text, text[:600]
    page.get_by_role("button", name="Save").first.click()
    wait_idle(page, 1500)
    page.get_by_role("button", name="Delete").first.click()
    wait_idle(page, 1500)
    shot(page, "11b-memory")
    nav(page, "Settings")
    page.get_by_role("tab", name="People and invites").click()
    wait_idle(page)
    page.get_by_label("Their email (recommended)").fill("sister@example.com")
    page.get_by_role("button", name="Create invite").click()
    wait_idle(page, 2000)
    shot(page, "12-settings-invite")
    text = page.inner_text("body")
    assert "Invite created" in text and "invite=" in text and "sister@example.com" in text
    page.get_by_role("tab", name="Audit log").click()
    wait_idle(page)
    assert "board.run" in page.content() and "medication.add" in page.content()
    shot(page, "13-audit")

    # A reload keeps the sign-in and the patient (cookie-backed session).
    cookies = {c["name"]: c for c in page.context.cookies()}
    assert "fmb_session" in cookies, cookies
    page.goto(BASE)
    wait_idle(page, 2500)
    assert "Parent One" in page.content() and page.get_by_role("button", name="Sign out").count() == 1
    # Home-screen support: patched index.html and the served manifest.
    r = page.request.get(BASE + "/")
    assert 'rel="manifest"' in r.text() and "apple-mobile-web-app-capable" in r.text(), r.text()[:500]
    m = page.request.get(BASE + "/app/static/manifest.json")
    assert m.ok and m.json()["display"] == "standalone"
    assert page.request.get(BASE + "/app/static/icon-180.png").ok

    # Sign out clears the cookie, and a reload stays on the login page.
    page.get_by_role("button", name="Sign out").click()
    wait_idle(page, 1500)
    page.goto(BASE)
    wait_idle(page, 2500)
    assert page.get_by_role("button", name="Sign out").count() == 0
    assert "fmb_session" not in {c["name"] for c in page.context.cookies() if c["value"]}
    page.get_by_test_id("stForm").filter(has_text="Sign in").get_by_label("Email").fill("omer@example.com")
    page.get_by_label("Password").first.fill("correct horse battery")
    page.get_by_role("button", name="Sign in").click()
    wait_idle(page, 2000)
    assert "Parent One" in page.content()
    print("SMOKE OK")
    browser.close()
