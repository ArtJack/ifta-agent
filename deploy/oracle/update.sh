#!/usr/bin/env bash
# Deploy new code to the Oracle box: pull, rebuild, roll the services.
#
# Usage:   sudo bash deploy/oracle/update.sh            # pull + rebuild + restart
#          sudo bash deploy/oracle/update.sh --no-pull  # rebuild what's on disk
#
# Takes a snapshot first. A deploy that turns out to need rolling back is
# exactly when you want a backup from *before* it, and the nightly timer may be
# up to 24h stale.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_DIR="$PROJECT_ROOT/deploy/oracle"
PULL=1
[[ "${1:-}" == "--no-pull" ]] && PULL=0

say() { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo (systemctl + docker)"
[[ -f "$COMPOSE_DIR/.env" ]] || die "$COMPOSE_DIR/.env missing — see deploy/oracle/install.sh"

say "pre-deploy snapshot"
systemctl start ifta-backup || die "backup failed — refusing to deploy on top of an unbacked-up state"
ok "snapshot taken"

if [[ $PULL -eq 1 ]]; then
    say "pulling latest code"
    git -C "$PROJECT_ROOT" pull --ff-only
fi

say "rebuilding image"
(cd "$COMPOSE_DIR" && docker compose build)

say "rolling services"
# `up -d` recreates only what changed; postgres keeps running untouched.
(cd "$COMPOSE_DIR" && docker compose up -d --remove-orphans)

say "waiting for web health"
for _ in $(seq 1 30); do
    if (cd "$COMPOSE_DIR" && docker compose exec -T web curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1); then
        ok "web healthy"
        break
    fi
    sleep 2
done

(cd "$COMPOSE_DIR" && docker compose ps)
say "pruning dangling images"
docker image prune -f >/dev/null
ok "deploy complete. Logs: journalctl -u ifta -f"
