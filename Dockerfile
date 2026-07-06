# powerbi-builder container image.
#
# Provides a reproducible runtime for the ADK web chat UI + /health endpoint
# (adk/server.py mounts the real `adk web` FastAPI app -- same as running
# `adk web adk/` -- plus /health and this project's A2A routes in one
# process). The interactive REPL and single-shot CLI are also available by
# overriding CMD. Secrets (GOOGLE_API_KEY) and persistence
# (POWERBI_SESSION_DB_URL) are supplied via env at run time.
#
# Build:  docker build -t powerbi-builder .
# Run:    docker run -p 8000:8000 --env-file .env -v $(pwd)/output:/app/output powerbi-builder
# Chat UI: open http://localhost:8000
# Health:  curl http://localhost:8000/health

FROM python:3.10-slim AS base

# Avoid writing .pyc files and force unbuffered logs (so `docker logs` is live).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install OS deps: pandas/openpyxl need libglib/libxml; uvicorn is pure-python.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 \
        libxml2 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (cached layer unless requirements change).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the project source. .dockerignore excludes output/, logs/, .env, etc.
COPY . .

# Create the output + logs directories so the container can write to them
# even when no volume is mounted (a bind mount is recommended for output).
RUN mkdir -p /app/output /app/logs

# Default config: serve the adk web chat UI + /health (adk/server.py).
# Override CMD to run the REPL (`python chat.py`) or CLI (`python main.py ...`).
ENV POWERBI_SERVER_HOST=0.0.0.0 \
    POWERBI_SERVER_PORT=8000 \
    POWERBI_OUTPUT_ROOT=/app/output

EXPOSE 8000

# Lightweight health check against the /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=5).status==200 else 1)"

CMD ["python", "-m", "adk.server"]
