#!/usr/bin/env bash
# Stop everything started by start.sh / main.py and any orphaned workers.
#
# Usage:  bash stop.sh           # stop processes only
#         bash stop.sh --clean   # also wipe chromium profile dirs
#                                # (forces a Cloudflare re-solve next start)
set -uo pipefail
cd "$(dirname "$0")"

PID_FILE=".scraper.pid"
CLEAN_CACHE=0
for arg in "$@"; do
    case "$arg" in
        --clean|--clean-cache) CLEAN_CACHE=1 ;;
    esac
done

# ── 1. Kill PID-file process + its group (graceful, then SIGKILL) ───────
stop_process_tree() {
    local pid="$1"
    kill "$pid" 2>/dev/null || true
    local pgid
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    if [ -n "$pgid" ]; then
        kill -TERM -"$pgid" 2>/dev/null || true
    fi
    for _ in 1 2 3 4 5 6 7 8; do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
    done
    echo "PID $pid did not exit after 8s — SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
    [ -n "$pgid" ] && kill -9 -"$pgid" 2>/dev/null || true
}

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping main.py (PID $PID) + process group…"
        stop_process_tree "$PID"
    else
        echo "PID $PID was stale."
    fi
    rm -f "$PID_FILE"
fi

# ── 2. Sweep by name (catches stale runs that lost their PID file, and
#       worker scripts started directly via `python src/<worker>.py`) ───
sweep() {
    local pattern="$1" label="$2"
    if pgrep -f "$pattern" >/dev/null 2>&1; then
        echo "  killing $label…"
        pkill -TERM -f "$pattern" 2>/dev/null || true
        sleep 2
        pkill -KILL -f "$pattern" 2>/dev/null || true
    fi
}

echo "Sweeping orphan workers + browsers…"
sweep "python.* main\.py"                "main.py"
sweep "python.* src/scraper\.py"         "scraper.py"
sweep "python.* src/ftp_upload\.py"      "ftp_upload.py"
sweep "python.* src/agent_number_finder\.py" "agent_number_finder.py"
sweep "xvfb-run"                         "xvfb-run wrappers"
sweep "\.pw-data-"                       "chromium (our profile dirs)"
sweep "\.pw-probe-"                      "chromium (probe profile dirs)"

# ── 3. Optional: wipe chromium profile dirs ─────────────────────────────
if [ "$CLEAN_CACHE" = "1" ]; then
    echo "Cleaning chromium profile dirs (you'll need to re-solve Cloudflare)…"
    rm -rf .pw-data-scraper .pw-data-scraper-* .pw-data-acct-* .pw-probe-* 2>/dev/null || true
fi

# ── 4. Sanity check ─────────────────────────────────────────────────────
LEFT=$(pgrep -fla "main\.py|src/scraper\.py|src/ftp_upload\.py|src/agent_number_finder\.py|\.pw-data-" 2>/dev/null || true)
if [ -n "$LEFT" ]; then
    echo "WARNING — these processes are still alive:"
    echo "$LEFT"
    exit 1
fi
echo "Done."
