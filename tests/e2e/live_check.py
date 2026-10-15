"""Exercise the full record flow against the configured database and model.

Creates a temporary account and a test patient, uploads a prescription and a
lab report, runs extraction with the configured provider, confirms the items,
runs the board, builds the brief, prints what happened, then deletes
everything it created (unless --keep is passed). Safe to run on the live
instance: it leaves no rows behind and the next real signup is still admin.

Run from the repo root with DATABASE_URL set:
  python tests/e2e/live_check.py            # real provider (needs a key)
  MODEL_PROVIDER=demo python tests/e2e/live_check.py
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pymupdf

import auth
import board
import brief as brief_mod
import database as db
import documents
import medications
from providers import get_provider

KEEP = "--keep" in sys.argv
TAG = secrets.token_hex(3)
EMAIL = f"livecheck-{TAG}@example.invalid"
PASSWORD = secrets.token_urlsafe(16)

RX_TEXT = [
    "City Family Clinic            Date: 2026-09-01",
    "Dr. Ayesha Khan, MBBS, FCPS (Medicine)",
    "Patient: Live Check Test Patient, 79 y, M",
    "Diagnosis: Type 2 diabetes mellitus; essential hypertension",
    "Rx",
    "1. Tab Metformin 500 mg      1 tablet twice daily after meals     3 months",
    "2. Tab Amlodipine 5 mg       1 tablet once daily in the morning   3 months",
    "3. Tab Atorvastatin 20 mg    1 tablet at night                    3 months",
    "Review in 3 months with HbA1c and lipid profile.",
]
LAB_TEXT = [
    "Chughtai Lab                  Report date: 2026-09-05",
    "Patient: Live Check Test Patient       Referred by: Dr. Ayesha Khan",
    "Test                 Result      Unit      Reference",
    "HbA1c                7.8         %         4.0 - 5.6",
    "Fasting glucose      142         mg/dL     70 - 100",
    "Creatinine           1.3         mg/dL     0.6 - 1.2",
    "Potassium            4.6         mmol/L    3.5 - 5.1",
    "LDL cholesterol      118         mg/dL     < 100",
]


def make_pdf(lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((60, y), line, fontsize=11)
        y += 18
    return doc.tobytes()


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


def main() -> int:
    t0 = time.time()
    provider = get_provider()
    print(f"provider: {provider.name}")
    step("migrations")
    print(db.run_migrations())

    step("signup temporary account")
    was_first = auth.is_first_user()
    invite_code = None
    if was_first:
        user = auth.signup(EMAIL, PASSWORD, "Live Check")
    else:
        # Someone real already exists; make an admin invite from the oldest admin so cleanup can delete it later.
        with db.db_cursor() as cur:
            cur.execute("SELECT * FROM users WHERE role = 'admin' AND password_hash IS NOT NULL ORDER BY created_at LIMIT 1")
            admin = cur.fetchone()
        inv = auth.create_invite(dict(admin), "admin")
        invite_code = inv["code"]
        user = auth.signup(EMAIL, PASSWORD, "Live Check", invite_code)
    print(f"user {user['id']} role {user['role']} (first user: {was_first})")

    pid = None
    try:
        step("create test patient")
        pid = db.create_patient(user, "ZZ Live Check (delete me)", age=79, sex="M")
        print(f"patient {pid}")

        step("upload prescription and extract")
        out = documents.ingest_upload(pid, user, "rx-livecheck.pdf", "application/pdf", make_pdf(RX_TEXT), provider, kind=None)
        print(json.dumps({k: v for k, v in out.items() if k != "item_ids"}, default=str, indent=1))
        if out.get("error"):
            raise RuntimeError(out["error"])

        step("upload lab report and extract")
        out2 = documents.ingest_upload(pid, user, "lab-livecheck.pdf", "application/pdf", make_pdf(LAB_TEXT), provider, kind="lab_report")
        print(json.dumps({k: v for k, v in out2.items() if k != "item_ids"}, default=str, indent=1))
        if out2.get("error"):
            raise RuntimeError(out2["error"])

        step("review queue")
        pending = documents.pending_review(pid, user)
        confirmed = 0
        for file_id, items in pending.items():
            for it in items:
                p = it["payload"]
                label = p.get("name") or p.get("test_name") or p.get("text") or p.get("label")
                print(f"  doc {file_id} p{it['page_number']} {it['item_type']:16s} conf {it['confidence']:.2f}  {label}")
                if it["item_type"] in ("medication", "lab_value") and (it["item_type"] != "lab_value" or p.get("numeric_value") is not None):
                    documents.confirm_item(it["id"], pid, user, p)
                    confirmed += 1
                else:
                    documents.reject_item(it["id"], pid, user)
        print(f"confirmed {confirmed} items")

        step("medication list and values after confirmation")
        for m in medications.list_medications(pid, user, status="active"):
            print(f"  {m['name']} {m.get('dose') or ''} {m.get('frequency') or ''} by {m.get('prescriber')} (doc {m['source_file_id']} p{m['source_page']})")
        for v in db.get_latest_vitals(pid, user):
            print(f"  {v['reading_type']}: {v['value']} {v['unit'] or ''} ref {v.get('reference_range')} {v.get('flag')} (doc {v['source_file_id']} p{v['source_page']})")

        step("board: record question")
        run = board.run_board(pid, user, "Summarize what the latest prescription and lab report show and what to ask Dr. Khan at the review.", provider)
        print(f"status {run['status']} agents {run['routed_agents']} tokens {run['input_tokens']}/{run['output_tokens']} cost {run['estimated_cost_usd']}")
        print(board.format_markdown(run)[:1500])

        step("board: advice-seeking question must be declined")
        run2 = board.run_board(pid, user, "Should we increase his metformin dose since the HbA1c is high?", provider)
        co = run2["coordinator_output"]
        print(f"declined: {co['declined_advice_request']} note: {co.get('decline_note')}")
        assert co["declined_advice_request"], "advice question was not declined"

        step("brief")
        b = brief_mod.build_brief(pid, user)
        md = brief_mod.brief_markdown(b, "en")
        pdf = brief_mod.brief_pdf(b, "en")
        print(md[:800])
        print(f"pdf bytes {len(pdf)}")
        print(f"\nALL STEPS OK in {time.time() - t0:.0f}s")
        return 0
    finally:
        if KEEP:
            print(f"\n--keep: leaving patient {pid} and user {EMAIL} in place")
        else:
            step("cleanup")
            if pid is not None:
                db.delete_patient(pid, user)
                print(f"deleted patient {pid}")
            with db.db_cursor(commit=True) as cur:
                cur.execute("DELETE FROM audit_log WHERE user_id = %s", (user["id"],))
                cur.execute("DELETE FROM invites WHERE used_by = %s OR code = %s", (user["id"], invite_code or ""))
                cur.execute("DELETE FROM users WHERE id = %s", (user["id"],))
            print(f"deleted user {EMAIL}; first-user state now: {auth.is_first_user()}")
            db.close_pool()


if __name__ == "__main__":
    sys.exit(main())
