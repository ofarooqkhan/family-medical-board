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
    page.get_by_label("Your name").fill("Omer")
    page.get_by_test_id("stForm").filter(has_text="Create account").get_by_label("Email").fill("omer@example.com")
    page.get_by_label("Password (at least 10 characters)").fill("correct horse battery")
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
    assert "Items awaiting review" in page.content()

    # Upload a prescription PDF in demo mode.
    nav(page, "Documents")
    page.get_by_test_id("stFileUploaderDropzoneInput").set_input_files({"name": "rx.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes()})
    wait_idle(page, 1500)
    page.get_by_role("button", name="Upload and extract").click()
    wait_idle(page, 4000)
    shot(page, "04-documents-review")
    html = page.content()
    assert "Check extracted items" in html and "Metformin (demo)" in html, "review items present"
    assert "Confirm all" in html

    # Confirm the high-confidence items, reject the other.
    page.get_by_role("button", name=re.compile("Confirm all")).click()
    wait_idle(page, 2500)
    html = page.content()
    assert "Metformin (demo)" not in html.split("All documents")[0] or "Amlodipine (demo)" in html
    for _ in range(5):
        reject = page.get_by_role("button", name="Reject")
        if not reject.count():
            break
        reject.first.click()
        wait_idle(page, 2000)
    shot(page, "05-documents-done")
    assert "Nothing waiting for review" in page.content()

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
    nav(page, "Settings")
    page.get_by_role("tab", name="People and invites").click()
    wait_idle(page)
    page.get_by_role("button", name="Create invite code").click()
    wait_idle(page, 1500)
    shot(page, "12-settings-invite")
    assert "It works once" in page.content()
    page.get_by_role("tab", name="Audit log").click()
    wait_idle(page)
    assert "board.run" in page.content() and "medication.add" in page.content()
    shot(page, "13-audit")

    # Sign out and back in.
    page.get_by_role("button", name="Sign out").click()
    wait_idle(page, 1500)
    page.get_by_test_id("stForm").filter(has_text="Sign in").get_by_label("Email").fill("omer@example.com")
    page.get_by_label("Password").first.fill("correct horse battery")
    page.get_by_role("button", name="Sign in").click()
    wait_idle(page, 2000)
    assert "Parent One" in page.content()
    print("SMOKE OK")
    browser.close()
