#!/usr/bin/env bash
# One-time setup for a Linux box: venv, deps, chromium, xvfb, dirs.
# Idempotent — safe to re-run.
#
# Usage:  bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON_BIN:-python3}"

echo "==> Setup starting in $(pwd)"
mkdir -p data logs

# ── Python venv ─────────────────────────────────────────────────────────
if [ ! -x .venv/bin/python ]; then
    echo "==> Creating venv at .venv"
    "$PY" -m venv .venv
fi
.venv/bin/pip install --upgrade pip wheel >/dev/null
echo "==> Installing requirements"
.venv/bin/pip install -r requirements.txt

# ── Chromium for Playwright/Patchright ──────────────────────────────────
echo "==> Installing Chromium for Playwright"
.venv/bin/python -m playwright install chromium
echo "==> Installing Chromium for Patchright (uses the same binary cache)"
.venv/bin/python -m patchright install chromium 2>/dev/null || true

# ── Xvfb (virtual display so headed Chrome can run on a headless server)
if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "==> xvfb-run not found — installing"
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y xvfb
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y xorg-x11-server-Xvfb
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y xorg-x11-server-Xvfb
    else
        echo "ERR: please install xvfb manually (e.g. apt-get install xvfb)" >&2
        exit 1
    fi
fi

# ── CSV templates (only if missing — never overwrite real data) ─────────
if [ ! -f data/accounts.csv ]; then
    echo "==> Creating data/accounts.csv template (add real accounts before running finder)"
    cat > data/accounts.csv <<'EOF'
username,password
EOF
fi
if [ ! -f data/proxies.csv ]; then
    echo "==> Creating data/proxies.csv template"
    cat > data/proxies.csv <<'EOF'
server,username,password
EOF
fi

# ── .env sanity ─────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        echo "==> No .env — copying .env.example. EDIT IT before starting."
        cp .env.example .env
    else
        echo "WARN: no .env and no .env.example. You must create .env manually."
    fi
fi

echo
echo "==> Done."
echo "    Next:  bash start.sh"
echo "    Logs:  tail -f logs/scrape.log"
