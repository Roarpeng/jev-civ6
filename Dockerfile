# War-room container — single process: FastAPI app + in-process FireTuner bridge.
#
# Hot-reload dev flow: the repo is volume-mounted at /app (docker-compose),
# dependencies live in /opt/venv (outside the mount shadow), and uvicorn
# watches ONLY server/ + civ6-mcp/src — so editing code on the host restarts
# the app inside the container without touching journal.db/state artifacts.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer (rebuilt only when lockfiles change) — needs the civ6-mcp
# package sources for its editable install.
COPY pyproject.toml uv.lock ./
COPY civ6-mcp/pyproject.toml civ6-mcp/README.md civ6-mcp/
COPY civ6-mcp/src civ6-mcp/src
RUN uv sync --frozen --no-install-project

# App layer (all of it is normally shadowed by the volume mount anyway).
COPY . .

# game host/port are injected by compose (host.docker.internal + forwarder).
EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8081/api/mode', timeout=4)" || exit 1

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8081", \
     "--reload", "--reload-dir", "/app/server", \
     "--reload-dir", "/app/civ6-mcp/src", "--log-level", "warning"]
