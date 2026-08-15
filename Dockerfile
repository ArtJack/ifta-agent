# IFTA Agent — container image for Azure Container Apps and the Oracle Linux
# box (web / worker / telegram-bot).
#
# One image, three roles. The orchestrator overrides the command per role:
#   web           -> ifta web --host 0.0.0.0 --port 8000   (default CMD below)
#   worker        -> ifta worker
#   telegram-bot  -> ifta telegram-bot
#
# Build for Oracle (adds boto3 for R2 backup replication):
#   docker build --build-arg EXTRAS=oracle -t ifta:latest .
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

# `ifta backup` shells out to pg_dump on Postgres deployments, and pg_dump
# refuses to run against a *newer* server — so pin the client to the same major
# version the compose file pins for the server. Debian bookworm only ships v15,
# hence PGDG. curl stays in the image for the compose healthcheck.
ARG PG_MAJOR=16
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    install -d /usr/share/postgresql-common/pgdg; \
    # apt reads an ASCII-armored key directly via signed-by — no gnupg needed.
    curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    # Derive the suite from the base image rather than hardcoding "bookworm",
    # so a future python:3.12-slim rebase doesn't silently break the build.
    . /etc/os-release; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc]" \
         "https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends "postgresql-client-${PG_MAJOR}"; \
    rm -rf /var/lib/apt/lists/*

# Project metadata + source first so the (slow) dependency resolution layer is
# cached until pyproject or src changes. The extras add psycopg for the Postgres
# backend (both deployments) and, for `oracle`, boto3 for R2 replication. The
# base deps ship manylinux wheels, so no compiler toolchain is needed.
ARG EXTRAS=azure
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install -e ".[${EXTRAS}]"

# Tracked, non-PII app data that ships in the image: the IFTA regulations KB
# and seed rate matrices. Runtime/PII data (real client history, web
# submissions, traces, job DB) is NOT baked in — it arrives via Azure Files
# mounts / Postgres in production (see .dockerignore and deploy/azure).
COPY data ./data

# Drop root. Azure Files volumes mount world-writable, so uid 10001 can write
# to the mounted submissions/traces/clients paths. On the Oracle box the host
# directories are bind-mounted instead, so deploy/oracle/install.sh chowns them
# to 10001 and labels them for SELinux — see docs/ORACLE.md.
RUN useradd --create-home --uid 10001 ifta && chown -R ifta:ifta /app
USER ifta

EXPOSE 8000

# Default role = web API. `ifta web` binds 127.0.0.1 by default, so we pass
# 0.0.0.0 explicitly for container ingress. Container Apps health-probes /healthz.
CMD ["ifta", "web", "--host", "0.0.0.0", "--port", "8000"]
