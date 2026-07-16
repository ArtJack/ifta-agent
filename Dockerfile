# IFTA Agent — container image for Azure Container Apps (web / worker / telegram-bot).
#
# One image, three roles. Azure Container Apps overrides the command per app:
#   web           -> ifta web --host 0.0.0.0 --port 8000   (default CMD below)
#   worker        -> ifta worker
#   telegram-bot  -> ifta telegram-bot
#
# Why an EDITABLE install at a fixed /app rather than a plain site-packages
# install: the app resolves its data directory with
# `Path(__file__).resolve().parents[3]` (see rates.py, agent/tools.py), i.e. it
# expects the package to live at <root>/src/ifta and `data/` to sit next to it.
# Installing editable at /app keeps that contract intact so regulations.json,
# the rate cache, and client history resolve to /app/data (mounted from Azure
# Files in production).
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Project metadata + source first so the (slow) dependency resolution layer is
# cached until pyproject or src changes. `.[azure]` adds psycopg for the
# Postgres backend used on Azure; the base deps ship manylinux wheels, so no
# compiler toolchain is needed.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[azure]"

# Tracked, non-PII app data that ships in the image: the IFTA regulations KB
# and seed rate matrices. Runtime/PII data (real client history, web
# submissions, traces, job DB) is NOT baked in — it arrives via Azure Files
# mounts / Postgres in production (see .dockerignore and deploy/azure).
COPY data ./data

# Drop root. Azure Files volumes mount world-writable, so uid 10001 can write
# to the mounted submissions/traces/clients paths.
RUN useradd --create-home --uid 10001 ifta && chown -R ifta:ifta /app
USER ifta

EXPOSE 8000

# Default role = web API. `ifta web` binds 127.0.0.1 by default, so we pass
# 0.0.0.0 explicitly for container ingress. Container Apps health-probes /healthz.
CMD ["ifta", "web", "--host", "0.0.0.0", "--port", "8000"]
