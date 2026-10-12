#!/usr/bin/env bash
# Container entrypoint: apply migrations, start the optional API, run Streamlit.
set -euo pipefail

cd "$(dirname "$0")"

echo "[start] applying database migrations"
python -c "import database; print(database.run_migrations())"

# The FastAPI service is optional until api.py lands. It listens on loopback
# only; Streamlit talks to it inside the container. If uvicorn dies the
# container keeps running, because Streamlit is the process that holds PID 1.
if [ -f api.py ]; then
  echo "[start] starting FastAPI on 127.0.0.1:8001"
  uvicorn api:app --host 127.0.0.1 --port 8001 &
else
  echo "[start] api.py not present, skipping FastAPI"
fi

# XSRF protection and CORS are off because they were off on the Replit
# deployment this config was copied from (.replit and .streamlit/config.toml).
# Streamlit's XSRF check can reject form posts and file uploads when the app
# runs behind a proxy whose Host header differs from what the browser sent.
# Revisit once the app has a stable hostname on Render: try removing these two
# flags and confirm uploads still work.
echo "[start] starting Streamlit on port ${PORT:-5000}"
exec streamlit run app.py \
  --server.port "${PORT:-5000}" \
  --server.address 0.0.0.0 \
  --server.headless true \
  --server.enableCORS false \
  --server.enableXsrfProtection false
