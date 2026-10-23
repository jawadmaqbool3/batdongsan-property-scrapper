#!/usr/bin/env bash
# Show the running state of main.py + a quick DB summary.
#
# Usage:  bash status.sh
set -uo pipefail
cd "$(dirname "$0")"

PID_FILE=".scraper.pid"
LOG_FILE="logs/scrape.log"

echo "── Process ──────────────────────────────────────────────────"
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        ps -p "$PID" -o pid,stat,etime,command 2>/dev/null || true
        echo "Status: RUNNING (PID $PID)"
    else
        echo "PID $PID is stale (process gone). Removing $PID_FILE."
        rm -f "$PID_FILE"
        echo "Status: NOT RUNNING"
    fi
else
    echo "Status: NOT RUNNING (no $PID_FILE)"
fi
echo

echo "── Children (chromium / playwright) ─────────────────────────"
pgrep -fla "main.py|chromium|playwright|xvfb" 2>/dev/null \
    | grep -v "pgrep" | head -10 || echo "(none)"
echo

echo "── Last 20 log lines ($LOG_FILE) ────────────────────────────"
if [ -f "$LOG_FILE" ]; then
    tail -n 20 "$LOG_FILE"
else
    echo "(no log file yet)"
fi
echo

echo "── DB summary ───────────────────────────────────────────────"
if [ -x .venv/bin/python ]; then
    .venv/bin/python - <<'PY' 2>/dev/null || echo "(DB query failed — is MySQL up?)"
import os
from dotenv import load_dotenv; load_dotenv()
import pymysql
try:
    c = pymysql.connect(
        host=os.getenv("DB_HOST","localhost"),
        port=int(os.getenv("DB_PORT","3306")),
        user=os.getenv("DB_USER","root"),
        password=os.getenv("DB_PASSWORD",""),
        database=os.getenv("DB_NAME","property"),
        connect_timeout=5,
    )
    cur = c.cursor()
    for q, lbl in (
        ("SELECT COUNT(*) FROM scrapy",                                "total rows"),
        ("SELECT COUNT(*) FROM scrapy WHERE agent_id IS NOT NULL",    "with agent_id"),
        ("SELECT COUNT(DISTINCT agent_id) FROM scrapy WHERE agent_id IS NOT NULL", "unique agents"),
        ("SELECT COUNT(*) FROM scrapy WHERE phone_number IS NOT NULL", "with phone"),
        ("SELECT COUNT(*) FROM scrapy WHERE CAST(large_image_paths AS CHAR) LIKE '%batdongsan.com.vn%'", "images NOT migrated"),
    ):
        cur.execute(q); print(f"  {cur.fetchone()[0]:>7}  {lbl}")
    c.close()
except Exception as e:
    print("  (DB unreachable:", str(e)[:160], ")")
PY
else
    echo "(no .venv — run 'bash setup.sh')"
fi
