#!/usr/bin/env bash
# Install IFTA web + worker as launchd services on a Mac (mini).
#
# Usage:   bash deploy/install.sh            # install + start
#          bash deploy/install.sh uninstall  # stop + remove
#
# What it does:
#   1. Renders the plist templates (substitutes the absolute project path).
#   2. Writes them to ~/Library/LaunchAgents/.
#   3. Loads (or unloads) the agents via launchctl.
#   4. Tails the most recent log lines so you can see the result.
#
# Re-run after pulling new code or rotating .env — it'll re-load cleanly.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
LOGS_DIR="$PROJECT_ROOT/logs"
AGENTS=(com.artjeck.ifta-web com.artjeck.ifta-worker)

ACTION="${1:-install}"

case "$ACTION" in
    install)   ;;
    uninstall) ;;
    *) echo "usage: $0 [install|uninstall]" >&2; exit 2 ;;
esac

# Sanity checks before touching launchd.
if [[ "$ACTION" == "install" ]]; then
    if [[ ! -x "$PROJECT_ROOT/.venv/bin/ifta" ]]; then
        echo "✗ .venv/bin/ifta not found — run 'python3.12 -m venv .venv && .venv/bin/pip install -e \".[dev]\"' first" >&2
        exit 1
    fi
    if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
        echo "⚠ $PROJECT_ROOT/.env missing — confirmation/packet emails will be disabled until you create it." >&2
    fi
fi

mkdir -p "$LAUNCH_AGENTS" "$LOGS_DIR"

unload_agent() {
    local label="$1"
    local plist="$LAUNCH_AGENTS/$label.plist"
    if [[ -f "$plist" ]]; then
        launchctl bootout "gui/$UID/$label" 2>/dev/null || true
        launchctl unload "$plist" 2>/dev/null || true
    fi
}

if [[ "$ACTION" == "uninstall" ]]; then
    for label in "${AGENTS[@]}"; do
        echo "→ unloading $label"
        unload_agent "$label"
        rm -f "$LAUNCH_AGENTS/$label.plist"
    done
    echo "✓ uninstalled. (Log files preserved in $LOGS_DIR.)"
    exit 0
fi

# install
for label in "${AGENTS[@]}"; do
    template="$PROJECT_ROOT/deploy/launchd/$label.plist.template"
    dest="$LAUNCH_AGENTS/$label.plist"
    if [[ ! -f "$template" ]]; then
        echo "✗ template not found: $template" >&2
        exit 1
    fi
    echo "→ rendering $label"
    sed "s|__PROJECT_ROOT__|$PROJECT_ROOT|g" "$template" > "$dest"

    unload_agent "$label"
    launchctl bootstrap "gui/$UID" "$dest"
    launchctl enable "gui/$UID/$label"
done

echo ""
echo "✓ installed. Agents loaded:"
launchctl list | awk '$3 ~ /com\.artjeck\.ifta-/ {printf "    %s  (pid=%s)\n", $3, $1}'

echo ""
echo "Tail logs with:"
echo "  tail -f $LOGS_DIR/web.{out,err}.log"
echo "  tail -f $LOGS_DIR/worker.{out,err}.log"
echo ""
echo "Health check:"
echo "  curl -s http://127.0.0.1:8000/healthz"
