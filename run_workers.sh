#!/usr/bin/env bash
# Launch N translate.py workers in the background with auto-restart.
# Safe to close SSH after running — workers keep going.
#
# Usage:
#   ./run_workers.sh            # default: 10 workers
#   ./run_workers.sh 3          # custom worker count
#   ./run_workers.sh 10 stop    # kill all running workers
#   ./run_workers.sh status     # show which workers are alive
#   ./run_workers.sh tail       # live tail of all worker logs

set -euo pipefail

cd "$(dirname "$0")"

WORKERS="${1:-10}"
ACTION="${2:-start}"

# Allow `./run_workers.sh status` / `tail` / `stop` as the first arg too.
case "$WORKERS" in
  start|stop|status|tail) ACTION="$WORKERS"; WORKERS=10 ;;
esac

mkdir -p logs

stop_all() {
  echo "Stopping workers…"
  for f in logs/worker-*.pid; do
    [ -f "$f" ] || continue
    pid=$(cat "$f")
    if kill -0 "$pid" 2>/dev/null; then
      # Kill the supervisor loop AND its python child.
      pkill -P "$pid" 2>/dev/null || true
      kill "$pid" 2>/dev/null || true
      echo "  killed $f (pid $pid)"
    fi
    rm -f "$f"
  done
  echo "Done."
}

status() {
  alive=0; dead=0
  for f in logs/worker-*.pid; do
    [ -f "$f" ] || continue
    pid=$(cat "$f")
    if kill -0 "$pid" 2>/dev/null; then
      echo "  ALIVE  $f  (pid $pid)"
      alive=$((alive+1))
    else
      echo "  DEAD   $f  (pid $pid)"
      dead=$((dead+1))
    fi
  done
  echo "---"
  echo "alive: $alive   dead: $dead"
}

tail_logs() {
  shopt -s nullglob
  files=(logs/worker-*.log)
  if [ ${#files[@]} -eq 0 ]; then
    echo "No log files yet in logs/"
    exit 1
  fi
  tail -n 5 -f "${files[@]}"
}

start() {
  # Refuse to double-launch.
  for f in logs/worker-*.pid; do
    [ -f "$f" ] || continue
    pid=$(cat "$f")
    if kill -0 "$pid" 2>/dev/null; then
      echo "Workers already running. Run './run_workers.sh stop' first." >&2
      exit 1
    fi
    rm -f "$f"
  done

  # Require .venv — system python on the VPS doesn't have deep_translator etc.
  if [ ! -f ".venv/bin/activate" ]; then
    cat >&2 <<EOF
ERROR: .venv not found at $(pwd)/.venv

Create it first:
  apt update && apt install -y python3-venv python3-pip
  python3 -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt

Then re-run: ./run_workers.sh $WORKERS
EOF
    exit 1
  fi

  # Sanity-check the deps are actually installed in the venv (via activation,
  # so any tooling that respects VIRTUAL_ENV behaves the same as the workers).
  if ! ( source .venv/bin/activate && python -c "import deep_translator, pymysql, dotenv" ) 2>/dev/null; then
    cat >&2 <<EOF
ERROR: .venv exists but required packages are missing.

Install them:
  source .venv/bin/activate
  pip install -r requirements.txt

Then re-run: ./run_workers.sh $WORKERS
EOF
    exit 1
  fi

  echo "Using venv: $(pwd)/.venv  (activated per worker)"
  echo "Launching $WORKERS worker(s)…"

  for i in $(seq 0 $((WORKERS - 1))); do
    nohup bash -c "
      source .venv/bin/activate
      export PYTHONUNBUFFERED=1
      while true; do
        python -u src/translate.py --workers $WORKERS --worker $i >> logs/worker-$i.log 2>&1
        echo \"[\$(date '+%F %T')] worker $i exited, restart in 30s\" >> logs/worker-$i.log
        sleep 30
      done
    " > /dev/null 2>&1 &
    pid=$!
    echo "$pid" > "logs/worker-$i.pid"
    disown "$pid" 2>/dev/null || true
    echo "  worker $i  pid=$pid  log=logs/worker-$i.log"
  done

  echo
  echo "All $WORKERS workers launched. Safe to close SSH."
  echo "Check progress:  ./run_workers.sh tail"
  echo "Check status:    ./run_workers.sh status"
  echo "Stop everything: ./run_workers.sh stop"
}

case "$ACTION" in
  start)  start ;;
  stop)   stop_all ;;
  status) status ;;
  tail)   tail_logs ;;
  *) echo "Unknown action: $ACTION" >&2; exit 2 ;;
esac
