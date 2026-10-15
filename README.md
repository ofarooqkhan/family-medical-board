# Family Medical Board

A multi-agent advisory board for caregivers looking after aging parents. Specialist agents (neurology, cardiology, nephrology, endocrinology, radiology) plus a coordinator read the records a caregiver uploads (paper prescriptions, lab reports, imaging reports, DICOM files) and keep the family informed about what the records say and what to ask the treating doctors. It runs as a Streamlit app on Replit with Replit managed Postgres.

What it does for a family: documents (photos, PDFs, DICOM) are read into a review queue and nothing enters the record until a person confirms it; medication list with history; readings imported from spreadsheets, typed notes or a photo of a log; appointments with one-tap add to Google Calendar, Outlook or Apple Calendar and a WhatsApp share; doctor conversations recorded on the phone or uploaded, transcribed, the doctor identified, and the actions and medication changes listed for confirmation; a specialist board run like a multidisciplinary meeting (a chair triages the question, each specialist works from its own slice of the record, the specialists discuss each other's findings, the chair synthesizes one reply and carries the open items to the next review) that answers in one continuous conversation and lists questions for the treating doctors; a pre-visit brief in English or Urdu; every model working kept and every specialist's memory editable. Invite-only accounts, remembered sign-in, and add-to-home-screen on iPhone.

The app never gives medical advice. Agents summarize records and list questions for the treating doctor. They do not recommend doses, treatments, or changes to care. If any output reads as advice, that is a bug.

To run: create a virtualenv, `python3 -m pip install -r requirements.txt`, set `DATABASE_URL` and an OpenAI key (see `.env.example`), then `streamlit run app.py --server.port=5000 --server.address=0.0.0.0`. The schema is managed by Alembic and applied automatically at startup. Unit tests run with `python3 -m pytest tests/ -q` and need no database; set `TEST_DATABASE_URL` to a throwaway Postgres to run the database tests too. Project status, decisions, and the next step are in [HANDOFF.md](HANDOFF.md).

## Deploy

The family's instance runs at https://medical-board-history.replit.app (Replit Autoscale, managed Postgres, Replit OpenAI integration). The paths below are alternatives.

Three ways to get a running copy. Each uses the same Dockerfile and runs the
database migrations itself at startup.

**Render (one click, hosted, about 7 USD a month plus the model usage).**
Click the button, sign in to Render with GitHub, enter your `OPENAI_API_KEY`
when asked, and wait for the first deploy. Every later push to `main`
redeploys.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/ofarooqkhan/family-medical-board)

**Railway (one click, hosted).** New Project, Deploy from GitHub repo, pick
this repository, add a PostgreSQL service, set `DATABASE_URL` to its
connection string and `OPENAI_API_KEY`, set the start command to `./start.sh`.

**Your own machine (free, needs Docker Desktop).**

```bash
git clone https://github.com/ofarooqkhan/family-medical-board
cd family-medical-board
OPENAI_API_KEY=sk-... docker compose up --build
```

Then open http://localhost:5000. Phones on the same Wi-Fi can use the
machine's local address. Data lives in the `pgdata` Docker volume.

### Notes from the original deploy section
Locally with Docker: `export OPENAI_API_KEY=sk-...` then `docker compose up --build`. This starts Postgres 16 and the app on http://localhost:5000; migrations run on every start and data persists in the `pgdata` volume. `docker compose down -v` throws the database away.

On Render: push the repo to GitHub, then in the Render dashboard choose New, then Blueprint, and select the repo. `render.yaml` defines one Docker web service and one Postgres database and wires `DATABASE_URL` between them. Render prompts for `OPENAI_API_KEY` (required) and `ANTHROPIC_API_KEY` (not used yet, can be left blank) when the Blueprint is created; both can be changed later under the service's Environment tab. Every push to the connected branch redeploys.

Whoever signs up first on a fresh database becomes the admin and can invite the rest of the family from the app.

To try the whole flow without a model key, set `MODEL_PROVIDER=demo`; every extraction and board answer is then labeled as demo output. Set `OPENAI_API_KEY` and remove `MODEL_PROVIDER` for real runs.
