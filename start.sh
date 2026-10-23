#!/usr/bin/env bash
# Launch main.py in the background under Xvfb, surviving SSH logout.
#
# Reads RUN_SCRAPER / RUN_FTP_UPLOAD / RUN_FINDER from .env to decide
# which workers actually start. Refuses to start a second instance.
#
# Usage:  bash start.sh
# PID is written to .scraper.pid. Stop with `bash stop.sh`.
set -euo pipefail
cd "$(dirname "$0")"

PID_FILE=".scraper.pid"
LOG_FILE="logs/scrape.log"

mkdir -p logs

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Already running (PID $(cat "$PID_FILE"))."
    echo "Use 'bash stop.sh' first, or 'bash status.sh' to see what it's doing."
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "No .venv found. Run 'bash setup.sh' first."
    exit 1
fi

if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "xvfb-run not installed. Run 'bash setup.sh' first."
    exit 1
fi

# Touch the log file so tail -F works immediately.
: >> "$LOG_FILE"

echo "==> Starting main.py (Xvfb virtual display, background, nohup)"
echo "    Reading worker switches from .env:"
grep -E "^RUN_(SCRAPER|FTP_UPLOAD|FINDER)=" .env 2>/dev/null | sed 's/^/      /' || true

# nohup + setsid = survives SSH disconnect + detaches from controlling tty.
# xvfb-run -a   = auto-pick free display number.
# --server-args = a 1366x768x24 virtual screen, plenty for chromium.
nohup setsid xvfb-run -a \
    --server-args="-screen 0 1366x768x24" \
    .venv/bin/python main.py \
    >> "$LOG_FILE" 2>&1 &

PID=$!
echo "$PID" > "$PID_FILE"

# Give it 2s to fail fast on obvious config errors.
sleep 2
if ! kill -0 "$PID" 2>/dev/null; then
    echo "main.py failed to start. Last 30 log lines:"
    tail -n 30 "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
fi

echo "Started.  PID=$PID  log=$LOG_FILE"
echo
echo "Useful commands:"
echo "  tail -f $LOG_FILE       # watch live"
echo "  bash status.sh          # check if alive + last 20 log lines"
echo "  bash stop.sh            # stop main.py"
