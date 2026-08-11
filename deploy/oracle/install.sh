#!/usr/bin/env bash
# Bootstrap the IFTA stack on an Oracle Linux box (8 or 9).
#
# Usage:   sudo bash deploy/oracle/install.sh              # install + start
#          sudo bash deploy/oracle/install.sh uninstall    # stop + remove units
#          sudo bash deploy/oracle/install.sh doctor       # diagnose, change nothing
#
# What it does:
#   1. Installs Docker CE + the compose plugin from Docker's repo (Oracle Linux
#      ships podman; docker compose is what the unit files drive).
#   2. Creates the state directories owned by uid 10001 — the container's
#      unprivileged `ifta` user — and gives them an SELinux label Docker can
#      relabel with :Z.
#   3. Renders and installs the systemd units (substituting the project path).
#   4. Starts the stack and enables the nightly backup timer.
#
# Deliberately NOT done here: writing .env. Secrets are the operator's job —
# copy .env.example, fill it in, chmod 600, then re-run.
#
# Idempotent: safe to re-run after pulling new code or editing .env.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMPOSE_DIR="$PROJECT_ROOT/deploy/oracle"
ENV_FILE="$COMPOSE_DIR/.env"
UNIT_DIR="/etc/systemd/system"
UNITS=(ifta.service ifta-backup.service ifta-backup.timer)
CONTAINER_UID=10001

ACTION="${1:-install}"

say()  { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$ACTION" =~ ^(install|uninstall|doctor)$ ]] || die "usage: $0 [install|uninstall|doctor]"
[[ $EUID -eq 0 ]] || die "run with sudo — this installs packages and systemd units"

# --- uninstall --------------------------------------------------------------
if [[ "$ACTION" == "uninstall" ]]; then
    say "stopping timer + stack"
    systemctl disable --now ifta-backup.timer 2>/dev/null || true
    systemctl disable --now ifta.service 2>/dev/null || true
    for unit in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$unit"; done
    systemctl daemon-reload
    ok "units removed. Data in \$IFTA_STATE_DIR and the R2 bucket are untouched."
    echo "  To also drop local containers/volumes:  cd $COMPOSE_DIR && docker compose down -v"
    exit 0
fi

# --- read config (needed by both doctor and install) ------------------------
#
# Deliberately NOT `source`d. Compose parses .env with its own KEY=VALUE reader
# where the value is the rest of the line, verbatim — so the documented
#     RESEND_FROM_EMAIL=ArtJeck IFTA <ifta@artjeck.com>
# is valid there but is a *bash syntax error* ('<' is a redirect), which aborted
# this script before it did anything. Read the keys we need the same way compose
# does instead of handing operator config to the shell.
env_value() {
    local key="$1"
    [[ -f "$ENV_FILE" ]] || return 0
    sed -n "s/^[[:space:]]*${key}=//p" "$ENV_FILE" | tail -n 1 | sed -e 's/[[:space:]]*$//' \
        -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

state_dir="$(env_value IFTA_STATE_DIR)"; state_dir="${state_dir:-/var/lib/ifta}"
backup_dir="$(env_value IFTA_BACKUP_HOST_DIR)"; backup_dir="${backup_dir:-$state_dir/backups}"

# --- doctor -----------------------------------------------------------------
if [[ "$ACTION" == "doctor" ]]; then
    say "docker";      command -v docker >/dev/null && docker --version || warn "docker not installed"
    say "compose";     docker compose version 2>/dev/null || warn "compose plugin missing"
    say "selinux";     getenforce 2>/dev/null || echo "n/a"
    say "state dir";   ls -ld "$state_dir" 2>/dev/null || warn "$state_dir missing"
    say "env file";    [[ -f "$ENV_FILE" ]] && stat -c '%n mode=%a owner=%U' "$ENV_FILE" || warn "$ENV_FILE missing"
    say "units";       systemctl is-enabled ifta.service 2>/dev/null || warn "ifta.service not enabled"
    say "timer";       systemctl list-timers --all ifta-backup.timer --no-pager 2>/dev/null | head -3
    say "containers";  (cd "$COMPOSE_DIR" && docker compose ps 2>/dev/null) || true
    exit 0
fi

# --- preflight --------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
    die "$ENV_FILE missing. Run:
    install -m 600 $COMPOSE_DIR/.env.example $ENV_FILE
    \$EDITOR $ENV_FILE     # fill in secrets, then re-run this script"
fi
mode="$(stat -c '%a' "$ENV_FILE")"
if [[ "$mode" != "600" ]]; then
    warn ".env is mode $mode — tightening to 600 (it holds the DB password and API keys)"
    chmod 600 "$ENV_FILE"
fi
for required in POSTGRES_PASSWORD ANTHROPIC_API_KEY RESEND_API_KEY CLOUDFLARE_TUNNEL_TOKEN; do
    value="$(env_value "$required")"
    [[ -n "$value" && "$value" != REPLACE* ]] || die "$required is unset or still a placeholder in $ENV_FILE"
done

# --- 1. docker --------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    say "installing Docker CE (Oracle Linux ships podman; the units drive docker compose)"
    dnf -y install dnf-plugins-core
    # Docker publishes no Oracle Linux repo; the CentOS build matches the RHEL base.
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    ok "docker present: $(docker --version)"
fi
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing — install docker-compose-plugin"
systemctl enable --now docker
ok "docker running"

# --- 2. state directories ---------------------------------------------------
say "creating state directories under $state_dir"
# `state` holds telegram_access.json; `postgres` is the DB volume. `clients`,
# `web_submissions` and `traces` are customer PII.
for sub in clients web_submissions traces state postgres; do
    install -d -m 0750 -o "$CONTAINER_UID" -g "$CONTAINER_UID" "$state_dir/$sub"
done
install -d -m 0750 -o "$CONTAINER_UID" -g "$CONTAINER_UID" "$backup_dir"
# 0750 on the parent so a non-root local account can't read customer data.
chmod 0750 "$state_dir"

if command -v getenforce >/dev/null 2>&1 && [[ "$(getenforce)" != "Disabled" ]]; then
    # container_file_t is what :Z applies per-mount; setting it on the tree up
    # front means a relabel is never needed mid-incident, and it survives a
    # `restorecon` because we register it in the policy rather than only on disk.
    if command -v semanage >/dev/null 2>&1 || dnf -y install policycoreutils-python-utils >/dev/null 2>&1; then
        semanage fcontext -a -t container_file_t "${state_dir}(/.*)?" 2>/dev/null || \
            semanage fcontext -m -t container_file_t "${state_dir}(/.*)?" 2>/dev/null || true
        restorecon -R "$state_dir" || true
        ok "SELinux: $state_dir labelled container_file_t"
    else
        warn "could not install policycoreutils-python-utils; relying on compose :Z labels only"
    fi
fi
ok "state directories ready (owner uid $CONTAINER_UID)"

# --- 3. firewall ------------------------------------------------------------
# Nothing to open: cloudflared makes an *outbound* connection and the app is
# never exposed on a host port. Warn if someone has already poked a hole.
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    if firewall-cmd --list-ports 2>/dev/null | grep -qE '(^|\s)(80|443|8000)/tcp'; then
        warn "firewalld exposes 80/443/8000 — the tunnel makes that unnecessary. Consider closing them."
    else
        ok "firewalld: no inbound ports needed (tunnel is outbound-only)"
    fi
fi

# --- 4. systemd units -------------------------------------------------------
say "installing systemd units"
for unit in "${UNITS[@]}"; do
    src="$COMPOSE_DIR/systemd/$unit"
    [[ -f "$src" ]] || die "unit template not found: $src"
    sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$src" > "$UNIT_DIR/$unit"
    chmod 0644 "$UNIT_DIR/$unit"
done
systemctl daemon-reload
ok "units installed"

# --- 5. start ---------------------------------------------------------------
say "building the image and starting the stack (first build takes a few minutes)"
systemctl enable --now ifta.service
systemctl enable --now ifta-backup.timer

echo ""
ok "installed."
(cd "$COMPOSE_DIR" && docker compose ps)
cat <<EOF

Next:
  Health (inside the box):   cd $COMPOSE_DIR && docker compose exec web curl -fsS localhost:8000/healthz
  Health (through tunnel):   curl -fsS \$IFTA_WEB_PUBLIC_BASE_URL/healthz
  Logs:                      journalctl -u ifta -f    |    cd $COMPOSE_DIR && docker compose logs -f web worker
  Backup now:                systemctl start ifta-backup && journalctl -u ifta-backup -n 30
  Enable Telegram:           set TELEGRAM_BOT_TOKEN in .env, then
                             cd $COMPOSE_DIR && docker compose --profile telegram up -d

Full runbook: docs/ORACLE.md
EOF
