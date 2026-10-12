# Family Medical Board

A multi-agent advisory board for caregivers looking after aging parents. Specialist agents (neurology, cardiology, nephrology, endocrinology, radiology) plus a coordinator read the records a caregiver uploads (paper prescriptions, lab reports, imaging reports, DICOM files) and keep the family informed about what the records say and what to ask the treating doctors. It runs as a Streamlit app on Replit with Replit managed Postgres.

The app never gives medical advice. Agents summarize records and list questions for the treating doctor. They do not recommend doses, treatments, or changes to care. If any output reads as advice, that is a bug.

To run: create a virtualenv, `python3 -m pip install -r requirements.txt`, set `DATABASE_URL` and an OpenAI key (see `.env.example`), then `streamlit run app.py --server.port=5000 --server.address=0.0.0.0`. The schema is managed by Alembic and applied automatically at startup. Unit tests run with `python3 -m pytest tests/ -q` and need no database; set `TEST_DATABASE_URL` to a throwaway Postgres to run the database tests too. Project status, decisions, and the next step are in [HANDOFF.md](HANDOFF.md).

## Deploy

Locally with Docker: `export OPENAI_API_KEY=sk-...` then `docker compose up --build`. This starts Postgres 16 and the app on http://localhost:5000; migrations run on every start and data persists in the `pgdata` volume. `docker compose down -v` throws the database away.

On Render: push the repo to GitHub, then in the Render dashboard choose New, then Blueprint, and select the repo. `render.yaml` defines one Docker web service and one Postgres database and wires `DATABASE_URL` between them. Render prompts for `OPENAI_API_KEY` (required) and `ANTHROPIC_API_KEY` (not used yet, can be left blank) when the Blueprint is created; both can be changed later under the service's Environment tab. Every push to the connected branch redeploys.

Whoever signs up first on a fresh database becomes the admin and can invite the rest of the family from the app.
