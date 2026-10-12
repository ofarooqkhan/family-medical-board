# Family Medical Board: Streamlit UI plus an optional FastAPI sidecar.
# Build:  docker build -t family-medical-board .
# Run:    see docker-compose.yml (needs DATABASE_URL and an OpenAI key).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=5000

# curl is only here for the HEALTHCHECK below. pymupdf and psycopg2-binary
# ship manylinux wheels, so no build tools or client libraries are needed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --user-group app

WORKDIR /app

# Install dependencies before copying the app so this layer is cached until
# requirements.txt changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# app.py creates patient_data_uploads/ in the working directory at import
# time, so the whole tree is owned by the non-root user.
COPY --chown=app:app . .
RUN chmod +x /app/start.sh \
    && mkdir -p /app/patient_data_uploads \
    && chown app:app /app/patient_data_uploads

USER app

EXPOSE 5000

# Streamlit answers 200 on /_stcore/health once the server is up. Migrations
# run before Streamlit starts, hence the long start period.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/_stcore/health" || exit 1

CMD ["./start.sh"]
