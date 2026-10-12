# HANDOFF

Written 2026-10-12 at the end of the Step 0 audit session. Updated the same
day by the A0 session that finished the bootstrap.
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

### Done
- Step 0 audit, delivered and accepted. Findings in section 4.
- Replit connector, GitHub connector, and both repos verified reachable
  (see section 6 for the one transient failure).
- All eight decisions answered by the owner. Locked in section 4.
- Replit source snapshot saved to `replit_snapshot/` so this repo is
  self-contained.
- A0 bootstrap complete and committed: `app.py` rewritten from the snapshot
  with the eleven edits from the old section 8.2 (now listed in section 3),
  `utils.py`, `database.py`, `tests/test_utils.py` (20 tests, all passing),
  `requirements.txt`, `.env.example`, `.replit`, `.streamlit/config.toml`,
  `README.md`, `pytest.ini`. All six audit bugs closed in code.

### Not verified
A0 has not been run against a Postgres or deployed to Replit. `app.py`,
`utils.py`, and `database.py` pass `py_compile`; only `utils.py` has tests.
First run on Replit is part of item 1 (the migration must upgrade the live
database in place).

### Not started
Items 1 through 17. Nothing beyond A0.

### Git state
- Repo: `https://github.com/ofarooqkhan/family-medical-board` (public).
  One branch, `main`. All work goes there. Small commits, pushed often.
- Local checkout in this container: `/home/user/family-medical-board`.
- History on `main` up to `53fa39e`: `c198775` snapshot and handoff,
  `303d4c5` utils and database, `23c7ce0` and `55aff1e` handoff updates,
  `34001e7` email removal, `aadcfce` repo switch, `53fa39e` single branch.
- The A0 session was given the harness branch
  `claude/inspiring-galileo-3pdhym` as its push target and could not push to
  `main` without the owner's say in that session. The A0 commits are on that
  branch, which is a fast-forward of `main`. First thing for the next session:
  `git fetch origin && git checkout main && git merge --ff-only origin/claude/inspiring-galileo-3pdhym && git push -u origin main`,
  or the owner merges it on GitHub. Then delete the session branch.
- Retired: `ofarooqkhan/Omer-` (holds the same early commits, stale, do not
  push there) and the session branch
  `claude/multi-agent-medical-advisor-011SRXiTj76yxe1NZBeKNKyt` (merged into
  `main` and deleted at the owner's request for simplicity).
- Reference clone: `/home/user/medical-board-history`, one commit `206f97f`
  on `main`. Read only in practice. Byte-identical to the zip the owner
  uploaded. Stale legacy code. Do not develop here.
- Decision 8 (section 4) allows a final mirror push of `main` to
  `medical-board-history` at the very end, after an explicit go from the
  owner at that time.

---

## 3. Every file touched

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

### `database.py` (rewritten from Replit original)
Same eight tables, same seed agents, same function names where possible.
Changes from the original:
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

### `tests/test_utils.py`, `tests/__init__.py`, `pytest.ini`
20 pytest cases over `utils.py` only. Run with `python3 -m pytest tests/ -q`.

### `requirements.txt`, `.env.example`, `.replit`, `.streamlit/config.toml`, `README.md`
`requirements.txt` is the snapshot's dependency list without `docx>=0.2.4`
(bug 4) and with `pytest>=8`. `.replit` is a byte copy of the snapshot.

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

Environment: the A0 session had Python 3.13.16 at `python3` (the audit
session had 3.11.17; it varies per container, the app targets 3.11 on
Replit). Use `python3 -m pip`. A `.venv` with pytest only existed in the A0
container; recreate it. There is no Postgres in this container; database
tests cannot run here until a test database is arranged (item 2).
`DATABASE_URL` is unset here.

```bash
# Set up
cd /home/user/family-medical-board
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install pytest                # unit tests only
python3 -m pip install -r requirements.txt   # full app

# Unit tests (no database needed)
python3 -m pytest tests/ -q

# Syntax check without installing heavy deps
python3 -m py_compile utils.py database.py app.py

# Run the app locally (needs DATABASE_URL and ANTHROPIC_API_KEY or OPENAI_API_KEY)
streamlit run app.py --server.port=5000 --server.address=0.0.0.0

# Git
git status
git add -A
git commit -m "<message>"       # attribution trailer required, see section 7
git push -u origin main
# On network failure retry 4 times with 2s, 4s, 8s, 16s backoff.

# Replit live file read (load tool first)
# ToolSearch query: select:mcp__Replit__read_app_file
# then call with replId 0910c133-b692-4c80-b4d8-39d37884f87b and a path
```

On Replit the app runs with the command in `replit_snapshot/.replit`.

---

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

### 6.5 Partially verified
`utils.py`, `database.py`, and `app.py` pass `python3 -m py_compile`.
`utils.py` is covered by `tests/test_utils.py`. `database.py` and `app.py`
have not been executed against a database; that happens on the first Replit
run in item 1.

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

## 8. Next step: item 1 (Alembic, pool), then item 2

Before anything else, land the A0 commits on `main` (see Git state in
section 2). Then work directly on `main` in small commits, one per logical
change. No questions to the owner are needed for item 1.

### 8.1 Item 1 scope
- Add `alembic>=1.13` to `requirements.txt`. `alembic init alembic`, then
  `alembic/env.py` reads `DATABASE_URL` from the environment (no
  `sqlalchemy.url` in `alembic.ini`). Alembic needs SQLAlchemy; add
  `sqlalchemy>=2.0` too. psycopg2-binary is already present.
- Migration `0001_initial`: the eight tables exactly as
  `database.init_database` creates them today, including `recorded_at` and
  `source_file_id` on `vitals_history`, written with `op.create_table`. Seed
  the six default agents in the same migration with `op.bulk_insert` using
  `ON CONFLICT DO NOTHING` semantics (use `op.execute` with the INSERT so
  re-running on the live database is a no-op).
- Because the live Replit database already has these tables, the first run
  must not fail on "relation already exists". Use `alembic stamp head` logic:
  in `database.run_migrations()`, if `alembic_version` is absent but
  `patients` exists, stamp `0001` then upgrade; otherwise `upgrade head`.
  Show the owner this plan before running it against the live database
  (standing rule: no production touch without the migration plan shown).
- Replace `init_database()` body with `run_migrations()` calling Alembic's
  command API (`alembic.command.upgrade(config, "head")`) at startup. Keep
  `init_default_agents` only if the migration does not seed; prefer the
  migration.
- Connection pool: `psycopg2.pool.SimpleConnectionPool(1, 5, DATABASE_URL,
  cursor_factory=RealDictCursor)` created lazily; `get_db_connection()`
  returns a context manager that gets from and puts back to the pool. Every
  `conn.close()` in `database.py` becomes a put-back. Streamlit reruns on
  every interaction, so the pool must live in a module global, not session
  state.
- Done when: a fresh Postgres reaches the same schema as the old
  `init_database` produced (compare `\d` output or `information_schema`),
  and a Replit redeploy keeps row counts.

### 8.2 Testing item 1
A throwaway Postgres is needed. Options in order of preference: a Replit
test database (owner creates a second one), or `docker run postgres:16` if
the container has Docker, or `pip install testing.postgresql` with a local
`initdb` if available. Record which one worked here.

### 8.3 Then item 2
Remove the orphan rule in `_patient_visible_clause()` and
`get_all_patients_for_user_or_orphans`, delete
`migrate_orphan_patients_to_user`, and add the cross-patient leakage test
against the test database. Backfill: the owner's existing patient becomes
patient 1 under the owner's user; add patient 2. Show the UPDATE before
running it.
