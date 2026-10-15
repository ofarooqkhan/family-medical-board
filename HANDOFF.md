# HANDOFF

Written 2026-10-12 at the end of the Step 0 audit session. Rewritten the same
day by the session that built the working app (items 1 to 14 and 17 in a
first version).
Read this top to bottom before touching anything. Everything a fresh session
needs is here or in files this document names.

---

## 1. Goal and acceptance criteria

Build a multi-agent doctor advisory board for caregivers of two aging parents.
Specialist agents plus a coordinator keep caregivers informed. The app must
never give medical advice. Source records are paper prescriptions, lab reports,
and imaging reports (paper, PDF, or DICOM) photographed or uploaded by the
caregiver. Hosted on Replit with Replit managed Postgres.

Owner: ofarooqkhan (GitHub). Not a software engineer; wants decisions
explained in plain terms and recommended, not surveyed.

The spec, verbatim, with done-when criteria:

P0 Data and safety
1. Storage: Postgres via DATABASE_URL. Migration tooling. Done when data
   survives a redeploy and row counts match. (Data migration dropped, see 4.2.)
2. Multi-patient: patients table and patient_id on every table, query, prompt,
   chart. Backfill existing as patient 1, add patient 2. Done when no query
   runs without a patient scope and a test proves no cross-patient leakage.
3. Extraction: replace OCR plus regex with Claude vision/PDF extraction into a
   fixed JSON schema (structured outputs). Separate schemas for prescriptions
   (drug, dose, frequency, prescriber, date) and lab reports (test, value, unit,
   reference range, date). Done when every extracted field carries a
   confidence flag and a link to the source image.
4. Confirm before save: nothing extracted is stored until the caregiver reviews
   it next to the original image and confirms or corrects it.
5. Source traceability: originals in object storage, every stored value linked
   to file and page.
6. Auth: login, roles (admin, editor, viewer), audit log of who changed what.
   All API keys in environment secrets. Portable auth provider.

P1 Agent architecture
7. Service layer: agent logic out of the UI into a FastAPI service (patients,
   documents, medications, appointments, board runs).
8. Structured specialist output: one JSON schema for all specialists
   (observations, source references, confidence, open questions for the
   doctor). No free-text-only output.
9. Medication reconciliation agent: one canonical medication list per patient,
   prescriber and change history, flags items in more than one specialist's
   domain for the coordinator.
10. Coordinator routing: call only relevant specialists. Log calls and token
    cost per run.
11. Scope enforcement: output schema and system prompts restrict agents to
    summarizing records and listing questions for the doctor. Test set of
    prompts that try to elicit dosing or treatment advice. Done when all are
    declined and redirected to the treating doctor.

P2 Caregiver features
12. Appointment and test calendar with reminders, per patient.
13. Pre-visit brief: one page per appointment, recent changes, open questions.
    PDF export.
14. Bilingual output (English and Urdu) as a per-user setting.

P3 Surfaces
15. Remote MCP server over the API with OAuth. Read tools first, write tools
    behind confirmation.
16. Expo mobile client against the same API: patient switcher, timeline,
    camera capture into the extraction flow, notifications.
17. Model provider interface so a specialist can run on a different model.

Rules from the owner
- Branch per task, small reviewable commits.
- Tests for items 2, 3, 4, 11 before marking them done.
- Do not touch production data without showing the migration plan.
- Do not use em dashes in any output. Ever. Chat, code, commits, docs.

Owner's ordering note: after Step 0, items 1 and 2 first and in sequence.
Then 3 to 5 (extraction), 6 (auth), 7 to 11 (agents) as three parallel
workstreams. P2 and P3 depend on item 7.

Owner's later addition: the orchestration layer is the real key. Finish the
app with a basic layer first, then deep dive into orchestration and iterate.

---

## 2. Current state

### Done (all on `main`, CI green)
- Step 0 audit; A0 bootstrap; item 1 (Alembic, pool).
- Item 2: orphan rule gone. Access model is one family per deployment:
  every active user sees every patient; roles viewer, editor, admin gate
  writes; every query scoped by patient_id; leakage test in
  `tests/test_database.py::test_no_cross_patient_leakage`.
- Item 3: `extraction.py`, fixed pydantic schemas for prescriptions, lab
  reports, imaging reports, other; every item has page number, confidence,
  and the fields the model was unsure about. PDF pages rendered by pymupdf,
  images normalized, DICOM pixel data rendered with its header as text.
- Item 4: `documents.py` queues every extracted item as pending; the
  Documents page shows each item beside its page image; `confirm_item` is
  the only path into medications or vitals_history.
- Item 5: originals and page PNGs stay in Postgres (`patient_files.file_data`,
  `document_pages.image_png`) behind `database.save_document`; every stored
  value carries `source_file_id`, `source_page`, `extracted_item_id`.
  Object storage was dropped with Replit; revisit only if the database grows.
- Item 6: `auth.py`, built-in accounts (bcrypt), first signup is admin,
  invite codes with role, `audit_log` on every write, admin screens for
  people and invites. Clerk dropped at the owner's request.
- Item 7: `api.py` FastAPI over the same service functions, bearer tokens.
- Items 8, 9, 10, 11: `board.py`. Shared `SpecialistOutput` schema; pharmacist
  gets deterministic duplicate and multi-prescriber flags from
  `medications.find_duplicates`; keyword routing with whole-word domains,
  small-model router as fallback, full review on request; `board_runs` logs
  calls, tokens, cost; `detect_advice_request` plus output scrubbing;
  twenty advice prompts in `tests/test_board.py` all declined.
- Items 12, 13, 14: `appointments.py` (reminders, ICS), `brief.py`
  (markdown and PDF, English or Urdu labels), per-user language setting
  applied to board output and briefs.
- Item 17: `providers.py`. OpenAI implemented; `demo` provider for runs
  without a key; Anthropic slot documented, not implemented (no key yet).
- Deployment files: Dockerfile, start.sh, docker-compose.yml, render.yaml,
  GitHub Actions CI (tests on a Postgres service plus a Docker build).
- Streamlit UI rewritten as a multipage app (`app.py`). Playwright smoke
  test `tests/e2e/smoke.py` passes end to end in demo mode; screenshots in
  `docs/screenshots/`.

- Owner round 2 (2026-10-15, `4fa2f09`): prescription upload on the
  Medications page, readings import from CSV, Excel, typed text or a photo
  of a log (`values_import.py`, `vitals_log` document kind), admin delete
  patient, invites bound to an email with WhatsApp and mail share links
  (migration 0004).
- Owner round 3 (`9e9eb3c`, `ad25943`, `9a269cc`): remembered sign-in
  (sessions table, 30 day cookie, last patient per user, migration 0005);
  per-appointment Google Calendar, Outlook, .ics and WhatsApp buttons;
  appointment times entered in the device timezone and stored UTC
  (`st.context.timezone`); add-to-home-screen support (manifest, icons,
  Apple meta tags served from `static/` and patched into Streamlit's
  index.html once per process by `app._install_home_screen_support`).
- Owner round 4: doctor conversations. `consultations.py` stores a visit
  recording (upload or `st.audio_input`), transcribes through
  `Provider.transcribe` (diarizing model first, plain model fallback),
  summarizes with a fixed schema (doctor identified with confidence,
  turns by speaker, context, summary, decisions, actions, medication
  changes, follow-ups, open questions), and queues medication starts and
  dated follow-ups as pending items (`appointment` item type added to
  `confirm_item`). Migration 0006. Brief carries recent conversations.
- Owner round 6: every board run is kept with each specialist's output and
  can be opened on the Board page; after a run every specialist that took
  part writes a `review` memory tied to the run, a conversation writes a
  memory for general duty; the Memory page lists, edits, deletes and
  (admin) clears memories. Migration 0007 adds provenance columns.

### Not done
- Hosted on Replit (section 8.0). Render, Railway, and docker compose remain
  as alternatives in the README.
- Anthropic provider (item 17 second half): add `AnthropicProvider` in
  `providers.py` when the owner adds a key; the owner's model choice is
  Sonnet for extraction and specialists, Opus coordinator, Haiku router.
- Items 15 and 16 (MCP server, Expo client).
- Live data: the Replit database was never exported. If the owner wants it,
  one `pg_dump` in the Replit shell, then restore into the new database and
  run the app once (migrations stamp and upgrade it). Otherwise fresh.
- Urdu in the PDF uses the host's DejaVu font if present; Arabic script
  shaping is not verified. Markdown and on-screen Urdu are fine.
- Email or push reminders. Reminders are on-screen when due, plus ICS
  export to the phone's calendar.
- The `patient_data_uploads` directory and PWA tags from the Replit app
  are gone; add-to-home-screen still works through the browser.

### Git state
- Repo `https://github.com/ofarooqkhan/family-medical-board`, branch `main`,
  all work there. The harness session branch
  `claude/inspiring-galileo-3pdhym` is a stale pointer and can be deleted.
- Reference clone of the retired repo: `/home/user/medical-board-history`.
  Mirror push at the very end only, after a go (decision 4.8).

## 3. Every file touched

This section is the A0 and item 1 record. For the current layout read
the module docstrings; each top-level module starts with what it owns.
Modules: `app.py`, `api.py`, `auth.py`, `database.py`, `documents.py`,
`extraction.py`, `providers.py`, `demo.py`, `board.py`, `medications.py`,
`appointments.py`, `brief.py`, `utils.py`, `alembic/versions/0001..0003`.

All paths relative to `/home/user/family-medical-board`.

### `.gitignore`
Copied unchanged from the Replit and GitHub repo. Excludes `*.db`,
`patient_data_uploads/`, `.streamlit/secrets.toml`, `.env`, caches, IDE files.

### `utils.py` (new)
Pure helpers, no Streamlit or database imports, so they are unit testable.
- `DOCTOR_PATTERNS` dict and `parse_addressed_doctor(message, agent_types=None)`.
  Moved out of app.py. Uses word boundaries so "endo" no longer matches
  "endorse". Optional `agent_types` filter so only active agents can be
  addressed.
- `parse_reading_timestamp(value)` returns datetime or None. Tries ISO then
  nine common formats. None means "time unknown", callers must not substitute
  now().
- `normalize_extracted_readings(file_type, data)` turns the vision model's
  JSON array into rows ready for `vitals_history`: cgm to `glucose`,
  blood_pressure to `blood_pressure_systolic`, `blood_pressure_diastolic`,
  `heart_rate`, lab_results to `lab_<slug>` with reference range in notes.
  Rows without a numeric value are dropped. Non-list input returns [].
  This is the fix for audit bug 1 (extraction dead-ended in
  `patient_files.extracted_values`).
- `dev_user_id(name)` deterministic sha256-based id so the dev login maps the
  same name to the same user. Fix for audit bug 2.
- `json_dumps_safe(value)`.

### `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/`
Standard `alembic init` layout. `alembic.ini` holds no URL; `env.py` reads
`sqlalchemy.url` set in code by `database.run_migrations()` and falls back
to `DATABASE_URL`. `env.py` rewrites `postgres://` and `postgresql://` to
`postgresql+psycopg2://` because SQLAlchemy 2.1 maps a bare `postgresql://`
to psycopg 3, which is not installed. Revisions:
- `0001_initial_schema.py`: the eight tables exactly as the pre-Alembic
  `init_database()` built them (verified by `pg_dump --schema-only` diff,
  only `alembic_version` differs) and the six-agent seed with
  `ON CONFLICT DO NOTHING`.
- `0002_vitals_recorded_at.py`: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
  for `recorded_at` and `source_file_id`. IF NOT EXISTS so a database that
  already got these columns from the A0 `init_database()` upgrades too.
CLI use: `DATABASE_URL=... alembic upgrade head`, `alembic current`,
`alembic history`. New revision: `alembic revision -m "..."` then edit.

### `database.py` (rewritten from Replit original, then item 1)
Item 1 changes:
- `db_cursor(commit=False)` context manager is the only way to run SQL.
  Gets a connection from a module-global `psycopg2.pool.ThreadedConnectionPool`
  (`POOL_MIN=1`, `POOL_MAX=8`), commits or rolls back, returns the
  connection, discards it on socket-level errors. A `BoundedSemaphore(POOL_MAX)`
  makes callers wait up to `POOL_WAIT_SECONDS` instead of getting psycopg2's
  immediate `PoolError`. Threaded, not Simple, because Streamlit runs each
  session in its own thread (psycopg2 source: SimpleConnectionPool "can't
  be shared across different threads").
- `run_migrations()` replaces `init_database()` (kept as an alias). It calls
  `migration_plan()`: if `alembic_version` is absent and `patients` exists,
  `alembic stamp 0001` first, then `alembic upgrade head`. Returns the plan.
- `init_default_agents` removed; the seed lives in revision 0001.
- `close_pool()` for tests and shutdown. `get_db_connection()` kept as a
  plain unpooled connect for anything external.
Earlier A0 changes from the original:
- Every function that reads or writes patient data now takes `user_id` and
  checks access with `_assert_patient_access` or
  `_assert_conversation_access`, which raise `PermissionError`. Previously
  unscoped and now scoped: `update_patient`, `delete_patient`,
  `save_patient_file`, `get_file_data`, `save_vital_reading`,
  `save_doctor_memory`, `create_conversation`, `get_conversation`,
  `update_conversation_title`, `delete_conversation`,
  `save_chat_message_to_conversation`, `get_conversation_history`.
  Fix for audit bug 3.
- Removed: `get_all_patients`, `create_patient` (unowned), `save_chat_message`,
  `get_chat_history`, `clear_chat_history`, `get_doctor_agent`. All were dead
  or unscoped.
- `vitals_history` gains `recorded_at TIMESTAMP` (when the reading was taken,
  NULL if the document gave no usable time) and `source_file_id` FK to
  `patient_files`. Added with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` so an
  existing database upgrades in place. `timestamp` keeps meaning ingest time.
  All ordering uses `COALESCE(recorded_at, timestamp)`.
- `save_vital_reading` signature: `(patient_id, user_id, reading_type, value,
  unit, source, notes, raw_data, recorded_at, source_file_id)`. Returns row id.
- New `save_extracted_readings(patient_id, user_id, file_id, filename, rows)`
  persists the output of `utils.normalize_extracted_readings`.
- `get_doctor_agents(active_only=True)`.
- `update_doctor_agent` accepts `is_active` for the per-agent toggle.
- `get_user_patients` added (owned only). `get_all_patients_for_user_or_orphans`
  kept for A0.
- Orphan rule (`OR p.user_id IS NULL`) is centralized in
  `_patient_visible_clause()` and kept for A0. Item 2 removes it in one place.
- `migrate_orphan_patients_to_user` kept, flagged for removal in item 2.
- `get_db_connection` raises `RuntimeError` if `DATABASE_URL` is unset.
- `SPECIALIST_ORDER` constant exported for app.py.

### `app.py` (rewritten from `replit_snapshot/app.py`)
Edits applied, nothing else changed:
1. `parse_addressed_doctor` removed; imported from `utils` and called with
   `agent_types=agents.keys()`.
2. Dead `render_chat_interface` and `render_patient_management` deleted.
3. Dev login uses `dev_user_id(name)`; `uuid` import dropped.
4. Every scoped `db.` call passes `user_id`; `user_id=None` defaults removed
   from `render_*`, `get_patient_context`, `run_consultation`.
5. `render_files_tab` pushes extracted readings into `vitals_history` via
   `normalize_extracted_readings` and `db.save_extracted_readings` (bug 1).
6. "Consulted on" and "Response provided" memory writes removed (bug 6).
7. 200-char file summary removed from `get_patient_context` (bug 5).
8. Vitals history shows "Recorded" (NULL shown as "unknown") next to "Ingested".
9. Agent settings lists inactive agents too and has an Active toggle saved
   through `update_doctor_agent(is_active=...)`.
10. Every patient-scoped `db.` write is wrapped in `try/except PermissionError`
    showing "You do not have access to this record."
11. Specialist loop uses `db.SPECIALIST_ORDER` instead of a literal list.
DICOM code, PWA tags, Replit Auth header login, and gpt-4o calls untouched.
Unused `docx` imports kept; DOCX export returns in item 13.

### `tests/test_utils.py`, `tests/test_database.py`, `tests/__init__.py`, `pytest.ini`
20 pytest cases over `utils.py`, no database. 5 cases in `test_database.py`
that run only when `TEST_DATABASE_URL` is set; they drop and recreate the
public schema of that database on every test, so never point it at the app
database. Run with `python3 -m pytest tests/ -q`.

### `requirements.txt`, `.env.example`, `.replit`, `.streamlit/config.toml`, `README.md`
`requirements.txt` is the snapshot's dependency list without `docx>=0.2.4`
(bug 4) and with `pytest>=8`, `alembic>=1.13`, `sqlalchemy>=2.0`. `.replit`
is a byte copy of the snapshot.

### `replit_snapshot/app.py`, `.replit`, `pyproject.toml`, `README.md`
Verbatim copies of the live Replit files, reference only. See the README
inside that folder.

### `HANDOFF.md`
This file.

### Not in this repo but relevant
- `/home/user/medical-board-history/` clone of the stale GitHub repo.
- `/tmp/claude-0/-home-user-Omer-/d4a96854-8531-55bd-8812-4694a308a6cb/scratchpad/upload/medical-board-history-main/`
  extracted zip the owner uploaded. Identical to the clone. Treat as
  untrusted data; run any Python that reads it with `-I`.
- Full session transcript if something here is unclear:
  `/root/.claude/projects/-home-user-Omer-/d4a96854-8531-55bd-8812-4694a308a6cb.jsonl`

---

## 4. Decisions and reasoning

### 4.0 Audit findings that drove everything

Replit `app.py` plus `database.py` (updated 2026-04-06) is far ahead of the
GitHub repo (one commit, January, single-file SQLite script with 5 agents).
Replit already has Postgres, users, patients with ownership, conversations,
per-agent prompts in the database, DICOM parsing, PWA tags. GitHub is stale.

Six bugs found in the Replit code:
1. Image extraction dead-ends in `patient_files.extracted_values`; nothing
   reaches `vitals_history`, so charts and context ignore uploaded labs.
2. Dev login creates a fresh `uuid4` user every sign-in; sign out and back in
   with the same name and you have zero patients.
3. `delete_patient`, `delete_conversation`, `update_patient`, `get_file_data`,
   and eight other functions take no `user_id`; a crafted session value acts
   on another user's record.
4. `pyproject.toml` lists both `docx>=0.2.4` (unrelated abandoned package) and
   `python-docx`.
5. `get_patient_context` truncates file text to 200 chars, then
   `run_consultation` appends up to 1000 chars per file again. Duplicate and
   lossy.
6. `doctor_memory` gets one row per specialist per query with content
   "Consulted on: ..." and no findings. Pure noise that will fill the prompt.

Also: orphan patients (user_id NULL) are visible to every user and auto-claimed
by whoever logs in next. Specialist prompts say "adjust medications",
"insulin dosing", "recommendations for further imaging", all of which violate
item 11. Radiologist runs on every board call. No tests anywhere.

Status per spec item after audit: 1 mostly done (Postgres yes, no migrations,
no pooling). 2 partial. 3 partial (GPT-4o vision to JSON, no schema, no
confidence, no source link). 4 missing. 5 partial (bytes in BYTEA, no per
value link). 6 partial (Replit Auth in prod, insecure dev login, no roles, no
audit). 7, 8, 9 missing. 10 partial (keyword routing, no cost log). 11
missing and prompts contradict it. 12 to 17 missing. DOCX export from the
legacy file was lost in the Replit rewrite.

### 4.1 Source of truth: Replit app.py and database.py
Reason: already has Postgres, ownership, conversations, DB-stored prompts.
Items 1, 2, 6 become hardening instead of greenfield.
Rejected: GitHub legacy file. Single patient, SQLite, no auth, three months
stale.

### 4.2 Data: fresh schema, no migration
Owner said "Fresh". Item 1 becomes: Alembic, schema as migration 0001, add a
connection pool, keep DATABASE_URL. No SQLite import script. The audit's
proposed migration script is dropped.
Rejected: migrating the SQLite file. There is no production data worth
keeping.

### 4.3 Radiologist: keep
Owner corrected the spec: records can be paper, PDF, or digital MRI, X-ray, CT.
The radiologist stays as a specialist and the DICOM code stays live. Its
prompt gets rewritten in item 11 to summarize and list questions, never
recommend.
Rejected: dropping it behind a feature flag. That was my recommendation and
the owner overruled it with the imaging requirement.

### 4.4 Roster
Default on: neurologist, nephrologist, cardiologist, spine, general duty,
endocrinologist (owner rejected renaming to "diabetes"), gastroenterologist,
radiologist, pharmacist, urologist, ophthalmologist.
Default off, with a toggle: pulmonologist, geriatric psychiatry, geriatrician.
Every agent gets an on/off toggle in the settings tab, backed by the existing
`doctor_agents.is_active` column.
Pharmacist is the item 9 medication reconciliation agent given a seat.
Rule the owner agreed to: one agent per treating doctor the parents actually
see, because every active agent costs tokens on every run until routing lands.

### 4.5 Auth: Clerk, invite only, Replit Auth kept as a second adapter
Owner delegated this. Reason: hosted login page (Google, Apple, email link),
invitation-only signup so no public registration, built-in roles mapping to
admin/editor/viewer, free tier covers a family, native Expo support for item
16. Replit Auth adapter stays so the deployed site works on day one.
Rejected: Auth0 (same result, more setup), Replit Auth alone (not portable,
tied to Replit accounts, no roles).

### 4.6 Models
Sonnet latest (claude-sonnet-5-5) for extraction and specialists. Opus
(claude-opus-5-5) for the coordinator synthesis. Haiku (claude-haiku-5-5) for
routing. Owner's words: "each model appropriate to the level of the task at
hand so we optimize for cost." All per-agent configurable via item 17's
provider interface. OpenAI kept as a second provider behind the same
interface. Will be revisited after the basic layer ships.

### 4.7 Object storage: Replit Object Storage behind an interface
Owner: optimize for cost and ease of use. Replit Object Storage is one click
to create a bucket, cents per month, and the env var appears automatically.
Existing BYTEA rows in `patient_files` get moved out in item 5.
Rejected: keeping bytes in Postgres (bloat, slow backups, pricier per GB),
S3 or GCS (more setup for no gain at this scale).

### 4.8 Where the code lands
Develop and push on `main` of `family-medical-board`. Originally the
designated target was a session branch on `Omer-`; the owner replaced the
repo and then asked for a single branch for simplicity. At the very end,
push `main` to `medical-board-history` too. The owner said yes to this
mirror in advance, but still confirm with a one-line "pushing the mirror
now" before doing it. Before that mirror push, run
`git -C /home/user/medical-board-history fetch origin main` so the push sends
a thin pack and is not rejected with HTTP 413.

### 4.9 Final architecture, approved by the owner
Everything on Replit.
- Replit Autoscale deployment, public URL, custom domain optional later.
- Streamlit web app (family UI, add-to-home-screen on phones).
- FastAPI service (item 7) in the same process group.
- Agent layer: specialists, coordinator, routing, cost log.
- Replit managed Postgres for structured data. Add a nightly export to object
  storage as a backup belt-and-braces.
- Replit Object Storage for originals.
- Replit Secrets for every key.
- Clerk for login, invite only.
- Phase 1 mobile: the website on the home screen. Phase 2: Expo (item 16).
Expected running cost: Replit plan plus roughly 30 to 80 USD a month in
Anthropic usage, kept down by routing (item 10).

### 4.10 Orchestration
Owner: build a basic orchestration layer first, ship the app, then deep dive
and iterate on the orchestration in versions. Do not over-engineer the
coordinator in the first pass.

### 4.11 A0 design choices
- `recorded_at` nullable, NULL means unknown. Rejected: substituting ingest
  time, which is what the old code did and which produces false timelines.
- Access checks raise `PermissionError` rather than silently returning None,
  so a missing check surfaces as an error in tests rather than empty data.
- Orphan rule kept in A0 and centralized, removed in item 2. Rejected:
  removing it in A0, because the deployed Replit app relies on it for the
  owner's existing patient and A0 must not break the running app.
- Pure helpers split into `utils.py` so A0 can ship tests without a Postgres
  instance. Database tests wait for item 2 when a test Postgres is set up.

### 4.12 Proposed execution order after A0 (approved)
Phase A, sequential: A0, then item 1, then item 2.
Phase B, parallel after item 2: stream 1 is items 5, 3, 4 (storage before
extraction, extraction before confirm). Stream 2 is item 6. Stream 3 is item
7, then 8 and 11 together, then 9 and 10, with 17 pulled into 7.
Phase C after 7: items 12, 13, 14 (14 after 8).
Phase D after 7 and 6: items 15, 16.

---

## 5. Commands

Environment notes: Python 3.13 in the cloud container, 3.11 in the Docker
image. PostgreSQL 16 binaries and Docker CLI are present but no daemon;
start a throwaway cluster as in 6.6. Only GitHub's API and PyPI are
reachable from the container.

```bash
cd /home/user/family-medical-board
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -r requirements-dev.txt

# Tests without a database (database tests skip)
python3 -m pytest tests/ -q
# All tests (never point this at the app database; it drops the schema)
TEST_DATABASE_URL="postgresql://postgres@/fmb_pytest?host=/tmp&port=5433" python3 -m pytest tests/ -q

# Run the app locally in demo mode (no model key needed)
DATABASE_URL="postgresql://postgres@/fmb_dev?host=/tmp&port=5433" MODEL_PROVIDER=demo \
  streamlit run app.py --server.port 8501 --server.headless true
# With a real key: drop MODEL_PROVIDER and set OPENAI_API_KEY
# API alongside: uvicorn api:app --port 8001

# End-to-end smoke test against the running app (fresh database first)
CHROMIUM_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome python3 tests/e2e/smoke.py /tmp/shots

# Migrations by hand
DATABASE_URL=... alembic current
DATABASE_URL=... alembic upgrade head
DATABASE_URL=... alembic revision -m "describe change"   # then edit the file

# Local full stack with Docker (on a machine with a daemon)
docker compose up --build

# Git
git push origin main
```

Environment variables the app reads: `DATABASE_URL`, `OPENAI_API_KEY`
(or the Replit `AI_INTEGRATIONS_*` pair), `OPENAI_BASE_URL`,
`MODEL_PROVIDER` (openai, demo, fake), `MODEL_EXTRACTION`,
`MODEL_SPECIALIST`, `MODEL_COORDINATOR`, `MODEL_ROUTER`,
`MODEL_TRANSLATE`, `APP_SECRET` (API token signing), `PORT`.

## 6. Open bugs and verbatim errors

### 6.1 Resolved: push to GitHub works again after the owner reconnected
Mid-session every push to the then-working repo `Omer-` failed with:
```
remote: Claude doesn't have GitHub access to ofarooqkhan/Omer- for your organization. An org admin can install the Claude GitHub App at https://github.com/apps/claude/installations/select_target, or reconnect GitHub from claude.ai settings (https://claude.ai/customize/connectors?auth_start=github&auth_start_force=1) to re-link an existing installation
fatal: unable to access 'https://github.com/ofarooqkhan/Omer-/': The requested URL returned error: 403
```
The owner reconnected GitHub from claude.ai settings and the next push
succeeded. If this recurs, it is the same owner-side fix. The harmless warning `fatal: expected 'acknowledgments', received 'packfile'
... proceeding anyway with push` is the session git proxy and can be ignored.

### 6.2 gh CLI token invalid
```
github.com
  X Failed to log in to github.com using token (GH_TOKEN)
  - Active account: true
  - The token in GH_TOKEN is invalid.
```
`gh` cannot be used. Plain `git` works through the proxy. Do not need `gh`
unless a PR is requested; the owner has not requested one.

### 6.3 Direct GitHub access to medical-board-history is blocked outside the attach
```
remote: request blocked: no rule allows host "github.com"
fatal: unable to access 'https://github.com/ofarooqkhan/medical-board-history.git/': The requested URL returned error: 403
```
Resolved by `mcp__Claude_Code_Remote__add_repo` with owner `ofarooqkhan`,
repo `medical-board-history`, access `read`. The clone at
`/home/user/medical-board-history` succeeded. For the final mirror push,
call `add_repo` again with access `push`.

### 6.4 MCP servers flap
Every MCP server (Replit, Claude_Code_Remote, higgsfield, Gmail, and so on)
disconnected and reconnected several times during the session with:
```
SdkHttpError dialing https://api.anthropic.com/v2/[redacted]/REDACTED/mcp?... (CLIENT_HTTP_NOT_IMPLEMENTED)
```
Treat as transient. If a Replit tool is needed and unavailable, use
`replit_snapshot/` instead.

### 6.6 Throwaway Postgres in the cloud container
PostgreSQL 16 is installed but not running. `initdb` refuses to run as root
and the `postgres` user cannot traverse the scratchpad path, so the cluster
lives under `/tmp`:
```bash
PGBIN=/usr/lib/postgresql/16/bin; PGDATA=/tmp/fmb_pgdata
mkdir -p $PGDATA && chown postgres:postgres $PGDATA
su postgres -c "$PGBIN/initdb -D $PGDATA -A trust -U postgres"
su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l $PGDATA/log -o '-p 5433 -k /tmp' start"
psql -h /tmp -p 5433 -U postgres -c "CREATE DATABASE fmb_pytest;"
```
The doc sites `alembic.sqlalchemy.org` and `psycopg.org` do not resolve
from this container. The installed packages' docstrings and `alembic init`
templates were used as the reference instead.

### 6.5 Partially verified
`utils.py`, `database.py`, and `app.py` pass `python3 -m py_compile`.
`utils.py` and `database.py` are covered by tests against Postgres 16.
`app.py` has not been launched anywhere yet.

---

## 7. Standing instructions and preferences

Hard rules from the owner:
- No em dashes in any output. Chat, code comments, commit messages, docs,
  tests. Use a comma, colon, or a new sentence instead.
- Branch per task, small reviewable commits.
- Tests for items 2, 3, 4, 11 before marking them done.
- Do not touch production data without showing the migration plan first.
- Only push to `main` on `ofarooqkhan/family-medical-board`. Mirror to
  `medical-board-history` only at the end, after a go.
- "Branch per task" from the original spec is relaxed to "one commit or a
  few small commits per task on main" at the owner's request for simplicity.
  Reintroduce feature branches only if the owner asks.
- The app must never give medical advice. Agents summarize records and list
  questions for the treating doctor.

Communication preferences observed:
- Owner answers in short numbered replies and expects numbered questions.
- Owner delegates technical choices ("You decide", "You tell me what's
  required") and wants a recommendation with the reason, in plain terms, not
  a menu.
- Owner interrupts with short directives mid-task; act on them immediately.
- Owner asked for cost optimization at every layer: models, storage, tokens.
- Owner wants daily use and ease of use across family and friends, on phones.

Harness rules in force:
- Commit messages end with these two trailer lines:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01YEQENGWk2342jbi1tKb9jX
  ```
  The session URL changes per session; use the one the harness gives you.
- PR descriptions, if one is ever requested, end with:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)

  https://claude.ai/code/session_01YEQENGWk2342jbi1tKb9jX
  ```
- Do not create a pull request unless asked.
- Do not put a model identifier in any pushed artifact.
- Scratchpad for temp files: the path the harness prints at session start
  under `/tmp/claude-0/`.

Credentials and ids:
- Replit app: `medical-board-history`, replId
  `0910c133-b692-4c80-b4d8-39d37884f87b`, URL
  `https://replit.com/@omerfk1/medical-board-history`.
- Replit env vars the app reads: `DATABASE_URL`,
  `AI_INTEGRATIONS_OPENAI_API_KEY`, `AI_INTEGRATIONS_OPENAI_BASE_URL`.
- Replit Object Storage bucket: not created yet. Owner needs to click Tools,
  Object Storage, Create bucket in the Replit workspace when item 5 starts.
- Clerk account: not created yet. Owner creates it when item 6 starts.
- Anthropic API key: not in Replit Secrets yet. Needed when item 3 starts.

---

## 8. Next step: get it hosted, then real-model runs

### 8.0 Live on Replit (2026-10-15)
Published at https://medical-board-history.replit.app (Autoscale, replId
`0910c133-b692-4c80-b4d8-39d37884f87b`). The live database migrated to
revision 0003 with the five existing patients kept; duplicate dev-login
emails were cleared by the fixed revision. The workspace was synced to main at
`2220bc6` by Replit Agent (it needed Plan mode off in the Repl to apply
changes). The real OpenAI path was verified there: `classify()` on a
generated prescription PDF returned kind prescription, date 2026-09-01,
1419 input and 42 output tokens on gpt-4o through the Replit integration.
Full flow verified on the live
database with the real model on 2026-10-15 by `tests/e2e/live_check.py`
(temporary account and patient, deleted afterwards): a generated
prescription and lab report extracted with every item at confidence 1.0
and linked to document and page, confirmation filled the medication list
and values, the board answered a record question (pharmacist routed,
about 4000 tokens, 1.6 cents) and declined a dosing question, brief and
PDF rendered. Cost of the whole check: about 4 cents. One tuning note:
the pharmacist marked the record question as an advice request on its
own, so the redirect banner showed above a correct answer; loosen the
specialist prompt wording in item 11's next pass.
The owner's first signup failed with "account already exists" because the
kept legacy row had the owner's email and no password; signup now claims
such rows (`d2b1b62`), and a Forgot password tab works with a
`PASSWORD_RESET_SECRET` secret the owner sets in Replit.
Two production bugs found on Replit and fixed on main: structured outputs
on the 1.x OpenAI SDK (`13eb362`) and stale pooled connections dropped by
the hosted Postgres (`2220bc6`, ping plus keepalives). Keep the workspace
and main in step by pulling main in the Repl before each publish. Creating new Repls needs a subscription
the account lacks; updating this one works, slowly, through Replit Agent.
The connector's publish_app republishes after changes.

### 8.0h Round 7 live (2026-10-15)
`ddc4a49` (delete a wrong conversation from the top of its record, with
confirmation) and `ad6eac3` (summaries use the patient's first name)
were synced and republished from the connector; `publish_app` works
again now that the schemas match. Live deployment status: running.
The production live checks could not be run from the workspace: the
Replit Agent has no production connection string. Production is
exercised only through the published app itself.
Agent memory today: append-only notes per run (review per specialist,
board summary, consultation, confirmed diagnosis or finding), the top 15
by importance shown to every specialist in build_context, editable on
the Memory page. No consolidation, no per-agent retrieval, no feedback
loop yet; design questions put to the owner (task 19).

### 8.0g Conversations flow verified with the real model (2026-10-15)
`tests/e2e/make_visit_audio.py` speaks a doctor and family exchange with
the espeak-ng library from the `espeakng-loader` wheel (34 s, two
voices). On the Repl, `transcribe_check.py --ingest` with that clip and
the Replit OpenAI integration: gpt-4o-transcribe returned the words
exactly, the gpt-4o summary labelled speaker A as the doctor (0.9) and B
as family, listed three decisions, three actions and three medication
changes (amlodipine start, metformin continue, aspirin continue), queued
one pending medication item, confirmation put amlodipine 5 mg on the
list, the general duty memory and the audit rows were written, and the
temporary account and patient were removed. Cost about one cent per
conversation of this length. The diarizing model is rejected by the
gateway ("Unexpected audio response format: diarized_json") and the
fallback handles it. Exit 0, 24 seconds.

### 8.0f Published (2026-10-15, later)
The owner republished after `c9b8d90`. Replit's schema sync created the
new tables on production, the first page load ran the guarded
migrations, and production is at 0007. The published site serves the
new build (manifest route 200, login page with the new welcome line and
three tabs, no error). Not yet done on the live site: a real doctor
recording through Conversations (needs a human voice; the owner can do
this first) and `tests/e2e/live_check.py` against production (creates
and removes a temporary account and patient; run only with the owner's
go).

### 8.0e Republish and Replit's schema sync (2026-10-15)
Replit validates a republish by diffing the development schema against
production and applying its own SQL to production before the new build
starts. The first attempt failed validation ("DROP CONSTRAINT
doctor_memory_source_consultation_id_fkey does not exist") because the
development database had been left half way through a downgrade. The
owner cancelled. Fix in `c9b8d90`: migrations 0004 to 0007 skip tables,
columns and indexes that already exist, so after Replit's sync the app's
startup upgrade still completes and stamps the head. Never choose "Copy
your development database schema and data to production": it replaces
production data (the owner's accounts) with the development copy.
Sequence that works: restart the workspace app so development is at the
head, then Republish, then approve the review.

### 8.0d Live state at the end of 2026-10-15
- GitHub main `735b84b`, CI green. Repl workspace runs the same files
  (Replit Agent added a commit of its own on top; python files identical).
- Replit keeps two databases. Development (workspace preview) is at
  revision 0007 with the 4 legacy patients and no password accounts.
  Production (the published app, the owner's accounts) is at 0003. The
  app migrates production to 0007 the first time the new build starts
  there.
- The published site still runs the old build. `publish_app` from the
  connector is refused while the schema differs; the owner must press
  Publish in the Replit UI once and approve the schema review.
- Workspace preview while the Repl is awake:
  https://0910c133-b692-4c80-b4d8-39d37884f87b-00-1r5nsab7pa7xm.sisko.replit.dev
  (development database, separate from production).
- Transcription through the Replit OpenAI integration: `/audio/speech`
  is not offered, `diarized_json` is rejected, `gpt-4o-transcribe` works
  (checked with `tests/e2e/transcribe_check.py`, exit 0). Speaker roles
  come from the summary model.

### 8.0c Repl git remote (2026-10-15)
The Repl's `origin` pointed at the retired `medical-board-history`
repository, so fetches failed with an auth error and Replit Agent had
re-typed earlier changes by hand (its own commit `2c10324`). The remote is
now set to the public `family-medical-board` repository, which needs no
token. Sync command inside the Repl:
`git fetch origin main && git checkout -B main origin/main`.
Python 3.11 runs there and in CI: no nested same-quote f-strings
(`0734f61` fixed two).

### 8.0b Publishing after rounds 2 to 6 (owner action)
`publish_app` from the connector is refused once the schema changed:
"applying database changes to the live app needs a review that is only
available on the Replit website". The owner opens the Repl, presses
Publish, approves the database change review, and later republishes can
come from the connector again. Migrations 0004 to 0007 run at app start on
the live database. Pull main in the Repl first (Replit Agent did this for
`4fa2f09`; repeat for the latest main).
Transcription on Replit is not yet verified: the Replit OpenAI integration
may not route the audio endpoint. If a conversation shows "transcription
failed", set a direct `OPENAI_API_KEY` secret in the Repl (the provider
prefers it) and try again. The fallback chain is
`gpt-4o-transcribe-diarize`, `gpt-4o-transcribe`, `whisper-1`.
Streamlit on Autoscale: sessions live in one process. Keep the deployment
at one machine, or move to a Reserved VM, so the websocket always reaches
the process that holds the session. The remembered-sign-in cookie covers a
cold start: a new process signs the person back in from the cookie.

### 8.0a Earlier Replit notes (2026-10-12)
The session used the Replit connector to sync the repo into the existing
Repl `medical-board-history` (replId `0910c133-b692-4c80-b4d8-39d37884f87b`).
Result:
- Replit Agent pulled commit `b472af2` verbatim (app.py byte-identical),
  installed the dependencies from `pyproject.toml`, kept `DATABASE_URL` and
  the OpenAI integration variables, and started Streamlit on port 5000.
- The live database had several `users` rows with the same email from the
  old dev login, so revision 0003 failed on the unique email index. Alembic
  rolled it back; `alembic_version` is `0001` there. Commit `484e1ac` fixes
  the revision (tested on a legacy copy with duplicates).
- The agent then stopped responding to further instructions (connector
  reports phase `paused`), and creating a new Repl failed with
  `requires_active_subscription`. So the Replit account, not the code, is
  what blocks finishing there.
To finish on Replit, the owner needs two actions inside the Repl: pull
`main` (Git pane, or approve the paused agent task), then press Run. The
app will migrate the database (0002, then the fixed 0003) and show the login
page. Publish from the Replit UI (Autoscale is already set in `.replit`).
The old dev users keep their patients; none of them can sign in, so the
first real signup becomes admin.

### 8.1 Hosting elsewhere (owner action, one time)
Recommended: Render Blueprint. In the Render dashboard, New, Blueprint,
pick the GitHub repo `ofarooqkhan/family-medical-board`. `render.yaml`
creates the web service and a Postgres. Enter `OPENAI_API_KEY` when asked
(or leave it and run in demo mode by setting `MODEL_PROVIDER=demo`). First
start runs migrations on the empty database. Open the URL, create the
first account (it becomes admin), add the parents as patients, create
invite codes for the family under Settings.
`render.yaml` field names were written from memory because render.com was
unreachable from the build container; if the Blueprint sync rejects a
field, the Render UI names it. Alternatives with the same Dockerfile:
Railway, Fly.io, or `docker compose up` on any machine.

If the owner wants the session to deploy and verify directly, allow
`api.render.com` in the cloud environment's network settings and provide a
Render API key as an environment secret.

### 8.2 First real-model session
With `OPENAI_API_KEY` set: upload three real documents (a prescription, a
lab report, an imaging report), check the extraction quality, adjust the
prompts in `extraction.py` if fields are missed, then run a full board
review and read every specialist's output for any advice phrasing.
`tests/test_board.py` has a live test slot: add one marked to skip without
a key that sends the twenty advice prompts to the real model and asserts
`declined_advice_request`.

### 8.2a One-click paths
The README has a Deploy to Render button, Railway steps, and the
`docker compose up` command for a Mac. All three were written without
reaching those sites from the build container; the Docker image itself
builds in CI, so the Docker path is the most certain.

### 8.3 Then
1. Anthropic provider when the key arrives (section 2).
2. Replit data export if wanted (section 2).
3. Items 15 and 16.
4. Orchestration deep dive per 4.10: routing quality, memory of past
   findings, per-specialist models, cost caps per run.
