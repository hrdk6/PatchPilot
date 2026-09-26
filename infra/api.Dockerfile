# PatchPilot API + background worker.
#
# The Docker CLI is installed because the sandbox creates sibling containers
# through the host daemon; the daemon itself is never run inside this image.

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# git: cloning target repositories. docker-cli: creating sandbox containers.
# tini: PID 1. The service spawns git and docker processes; without an init,
# children orphaned by a timeout would never be reaped, and signals would not
# be forwarded for a graceful shutdown.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg tini \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first so source edits do not reinstall the world.
COPY pyproject.toml README.md ./
COPY packages/core/patchpilot_core/__init__.py packages/core/patchpilot_core/__init__.py
COPY packages/indexer/patchpilot_indexer/__init__.py packages/indexer/patchpilot_indexer/__init__.py
COPY packages/agent/patchpilot_agent/__init__.py packages/agent/patchpilot_agent/__init__.py
COPY packages/sandbox/patchpilot_sandbox/__init__.py packages/sandbox/patchpilot_sandbox/__init__.py
COPY packages/evals/patchpilot_evals/__init__.py packages/evals/patchpilot_evals/__init__.py
COPY apps/api/patchpilot_api/__init__.py apps/api/patchpilot_api/__init__.py
# The postgres extra ships the driver for PATCHPILOT_DATABASE_URL=postgresql+psycopg://…
# Without it the image could only ever use SQLite.
RUN pip install --no-cache-dir -e ".[postgres]"

COPY packages/ packages/
COPY apps/api/ apps/api/
COPY fixtures/ fixtures/
COPY docs/ docs/

RUN pip install --no-cache-dir -e ".[postgres]"

# The API needs to write only its data directory, which is owned by an
# unprivileged `patchpilot` user. The image does not switch to that user by
# default: creating sandbox containers needs the host Docker socket, whose group
# id differs from host to host, and access to that socket is root-equivalent
# anyway. Where the socket is not mounted (PATCHPILOT_SANDBOX_BACKEND=local, or a
# remote sandbox), run the service with `user: "10001:10001"`. docs/security.md
# covers the socket in more detail.
RUN useradd --create-home --uid 10001 patchpilot \
    && mkdir -p /data \
    && chown -R patchpilot:patchpilot /data /app

ENV PATCHPILOT_DATA_DIR=/data \
    PATCHPILOT_DATABASE_URL=sqlite+pysqlite:////data/patchpilot.db

EXPOSE 8000

# /ready, not /health: it fails while the database is unreachable, the schema
# is behind, or the embedded worker is down. (A container running
# `patchpilot worker` instead should override this healthcheck.)
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5).status == 200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]

# The CLI wires PatchPilot's own JSON logging into uvicorn; invoking uvicorn
# directly would reinstate its handlers and produce two log formats.
CMD ["patchpilot", "serve", "--host", "0.0.0.0", "--port", "8000"]
