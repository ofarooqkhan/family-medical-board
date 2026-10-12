# HANDOFF

Written 2026-10-12 at the end of the Step 0 audit session, mid A0 bootstrap.
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
- A0 partially written: `utils.py` and `database.py` exist on disk, uncommitted.
- Replit source snapshot saved to `replit_snapshot/` so this repo is
  self-contained.

### In progress: A0 bootstrap
A0 is the branch-zero commit that brings the Replit app into this repo, fixes
the six audit bugs, and adds tests and config. Nothing is committed yet.

Written, not yet verified by running:
- `utils.py` (new, complete)
- `database.py` (rewritten from the Replit original, complete)

Not yet written:
- `app.py` (rewritten from `replit_snapshot/app.py`, see section 8 for the
  exact edits)
- `tests/test_utils.py`
- `requirements.txt`
- `.env.example`
- `.replit` and `.streamlit/config.toml` (copy from snapshot)
- `README.md`
- `pytest.ini` or `pyproject.toml` with pytest config

Empty directories exist but git will not track them: `static/`, `tests/`,
`.streamlit/`.

### Not started
Items 1 through 17. Nothing beyond A0.

### Git state
- Repo: `https://github.com/ofarooqkhan/family-medical-board` (public).
  One branch, `main`. All work goes there. Small commits, pushed often.
- Local checkout in this container: `/home/user/Omer-` (old directory name,
  `origin` points at `family-medical-board`). A fresh session should select
  `family-medical-board` when it starts and work on `main`.
- History so far on `main`: `c198775` snapshot and handoff, `303d4c5` utils
  and database, `23c7ce0` and `55aff1e` handoff updates, `34001e7` email
  removal, `aadcfce` repo switch, plus the commit that records the move to a
  single branch.
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

All paths relative to `/home/user/Omer-`.

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

Environment: Python 3.11.17 at `python3`. `pip` on PATH is Python 3.13's, so
use `python3 -m pip`. No virtualenv exists yet. Nothing is installed yet.
There is no Postgres in this container; database tests cannot run here until
a test database is arranged (item 2). `DATABASE_URL` is unset here.

```bash
# Set up
cd /home/user/Omer-
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install -r requirements.txt   # file does not exist yet, see section 8

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
`utils.py` and `database.py` pass `python3 -m py_compile`. They have not been
executed against a database or covered by tests yet. Section 8.3 adds the
tests for `utils.py`. `database.py` can only be exercised once a Postgres is
available (item 2).

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
  Claude-Session: https://claude.ai/code/session_011SRXiTj76yxe1NZBeKNKyt
  ```
- PR descriptions, if one is ever requested, end with:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)

  https://claude.ai/code/session_011SRXiTj76yxe1NZBeKNKyt
  ```
- Do not create a pull request unless asked.
- Do not put a model identifier in any pushed artifact.
- Scratchpad for temp files:
  `/tmp/claude-0/-home-user-Omer-/d4a96854-8531-55bd-8812-4694a308a6cb/scratchpad`

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

## 8. Next step: finish A0 and push

Execute in this order. No questions to the owner are needed.

### 8.1 Verify what exists
```bash
cd /home/user/Omer-
python3 -m py_compile utils.py database.py
```
Fix any syntax error before continuing.

### 8.2 Write `app.py`
Start from `replit_snapshot/app.py`. Apply exactly these edits:

1. Delete the `parse_addressed_doctor` function. Add
   `from utils import parse_addressed_doctor, normalize_extracted_readings, dev_user_id`.
   In `render_chat_interface_with_conversations`, call
   `parse_addressed_doctor(user_input, agent_types=agents.keys())`.
2. Delete `render_chat_interface` and `render_patient_management` (dead code,
   never called from `main`).
3. In `render_login_page`, dev login path: replace
   `user_id = str(uuid.uuid4())` with `user_id = dev_user_id(name)`. Drop the
   `uuid` import if unused.
4. Every `db.` call that gained a `user_id` parameter (listed in section 3)
   must pass `user_id`. Specifically:
   `db.delete_conversation(conv['id'], user_id)`,
   `db.get_conversation(conversation_id, user_id)`,
   `db.update_conversation_title(conversation_id, user_id, title)`,
   `db.save_chat_message_to_conversation(patient_id, conversation_id, user_id, role, content, ...)`,
   `db.get_conversation_history(conversation_id, user_id, limit=50)`,
   `db.create_conversation(user_id, patient_id, title)` (unchanged order),
   `db.update_patient(patient_id, user_id, **fields)`,
   `db.delete_patient(patient_id, user_id)`,
   `db.save_patient_file(patient_id, user_id, filename, file_type, file_data, extracted_text, extracted_values)`,
   `db.get_file_data(f['id'], user_id)`,
   `db.save_vital_reading(patient_id, user_id, vital_type, value, unit, "Manual Entry", notes)`,
   `db.save_doctor_memory(patient_id, user_id, doctor_type, memory_type, content, importance=...)`.
   Remove the `user_id=None` defaults on `render_*` functions; `user_id` is
   always available from `main`.
5. In `render_files_tab`, after `file_id = db.save_patient_file(...)`, add:
   ```python
   if extracted_values is not None and file_type in ("cgm", "blood_pressure", "lab_results", "general"):
       rows = normalize_extracted_readings(file_type, extracted_values)
       n = db.save_extracted_readings(patient_id, user_id, file_id, file.name, rows)
       if n:
           st.info(f"{n} readings added to vitals history")
   ```
   This closes audit bug 1.
6. In `run_consultation`, delete the loop at the end that writes
   `"Consulted on: ..."` memories for every specialist, and delete the
   `"Query: ... Response provided."` memory in the addressed-doctor branch.
   Closes audit bug 6. Real findings memory is item 9's job.
7. In `get_patient_context`, delete the block that appends
   `f"  Summary: {f['extracted_text'][:200]}..."` per file. Keep the filename
   list. `run_consultation` already appends up to 1000 chars per file.
   Closes audit bug 5.
8. In `render_vitals_tab`, the history dataframe: show `recorded_at` next to
   `timestamp` and label the columns "Recorded" and "Ingested". If
   `recorded_at` is NaT, display "unknown".
9. In `render_agent_settings`, call `db.get_doctor_agents(active_only=False)`
   and add a `st.toggle("Active", value=agent['is_active'], key=f"active_{agent['agent_type']}")`
   whose value is passed as `is_active=` to `db.update_doctor_agent` on save.
   Wrap every `db.` write in the UI in `try/except PermissionError` that shows
   `st.error("You do not have access to this record.")`.
10. Leave the DICOM code, PWA tags, and Replit Auth header login as is.
11. Keep the gpt-4o calls for A0. Claude comes in with item 3 and item 17.

### 8.3 Write `tests/test_utils.py`
pytest. Cover at minimum:
- `parse_addressed_doctor`: "ask the cardiologist" returns cardiologist,
  "I endorse this" returns None (word boundary), agent_types filter excludes
  a match, empty string returns None.
- `parse_reading_timestamp`: ISO with Z, "2026-03-04 08:15", "04/03/2026",
  garbage returns None, None returns None, empty string returns None.
- `normalize_extracted_readings`: cgm list yields glucose rows with
  `recorded_at` set when timestamp present and None when absent; blood
  pressure yields three rows per reading when pulse present and two when
  not; lab_results yields `lab_hba1c` with "Reference:" in notes; non-list
  input yields []; readings with non-numeric value are skipped.
- `dev_user_id`: same name same id, case and whitespace insensitive,
  different names differ, starts with "dev-".
Also add `tests/__init__.py` (empty) and a `pytest.ini` with
`[pytest]\ntestpaths = tests`.

### 8.4 Write config files
- `requirements.txt` from `replit_snapshot/pyproject.toml` dependencies, with
  `docx>=0.2.4` removed (audit bug 4) and `pytest>=8` added. Keep the `>=`
  floors as they are.
- `.env.example` listing `DATABASE_URL=`, `OPENAI_API_KEY=`,
  `AI_INTEGRATIONS_OPENAI_API_KEY=`, `AI_INTEGRATIONS_OPENAI_BASE_URL=`,
  `ANTHROPIC_API_KEY=` with one comment line each.
- Copy `replit_snapshot/.replit` to `.replit` unchanged.
- `.streamlit/config.toml` with:
  ```
  [server]
  headless = true
  port = 5000
  address = "0.0.0.0"
  enableCORS = false
  enableXsrfProtection = false
  ```
- `README.md`: three paragraphs. What it is, that it never gives medical
  advice, how to run (section 5 commands). Link to HANDOFF.md for status.

### 8.5 Verify
```bash
python3 -m py_compile utils.py database.py app.py
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install pytest
python3 -m pytest tests/ -q
```
All tests must pass. Do not install the full requirements just to run unit
tests; `tests/test_utils.py` imports only `utils`.

### 8.6 Commit and push
Commits 1 and 2 below already exist as `c198775` and `303d4c5` (commit 2 is
partial, utils and database only). Add, in this order, each with the trailer
from section 7:
1. "Bootstrap app from Replit source with audit fixes" containing `app.py`,
   `requirements.txt`, `.env.example`, `.replit`, `.streamlit/config.toml`,
   `README.md`, and the HANDOFF.md state update. Body lists the six bugs
   fixed by number.
2. "Add unit tests for extraction helpers" containing `tests/`, `pytest.ini`.

Then:
```bash
git push -u origin main
```
Retry with 2s, 4s, 8s, 16s backoff on failure. Report the commit hashes and
the push result to the owner.

### 8.7 Then start item 1
Work directly on `main` in small commits, one per logical change.

Item 1 scope: add Alembic, `alembic/env.py` reading `DATABASE_URL`,
migration 0001 that creates the eight tables with the A0 columns, replace
`init_database()` CREATE IF NOT EXISTS calls with `alembic upgrade head` run
at startup, add `psycopg2.pool.SimpleConnectionPool` behind
`get_db_connection`. Done when a fresh Postgres reaches the same schema as
`database.init_database` would have produced, and a redeploy does not lose
rows.
