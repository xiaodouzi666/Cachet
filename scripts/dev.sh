#!/usr/bin/env bash
set -euo pipefail
umask 077

repo=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo"
api_port=${API_PORT:-8000}
web_port=${WEB_PORT:-3000}
python_bin=${PYTHON:-$repo/.venv/bin/python}
command -v tmux >/dev/null || { echo "Install tmux to start the development services." >&2; exit 1; }
"$python_bin" -c 'import fastapi, uvicorn'
test -x frontend/node_modules/.bin/next || { echo "Run npm ci --prefix frontend first." >&2; exit 1; }
mkdir -p logs

started=()
cleanup() {
  for session in "${started[@]}"; do
    tmux kill-session -t "=$session" 2>/dev/null || true
  done
}
trap cleanup ERR
start() {
  local session=$1
  shift
  if tmux has-session -t "=$session" 2>/dev/null; then
    if [[ $(tmux display-message -p -t "=$session:" '#{session_path}') != "$repo" ]]; then
      echo "The tmux session $session belongs to another checkout." >&2
      return 1
    fi
    return
  fi
  tmux new-session -d -c "$repo" -s "$session" env "PATH=$PATH" bash -c \
    'umask 077; log=$1; shift; exec "$@" >> "$log" 2>&1' _ "logs/$session.log" "$@"
  started+=("$session")
}

wait_for_url() {
  local session=$1 url=$2
  for ((attempt=1; attempt<=60; attempt++)); do
    tmux has-session -t "=$session" 2>/dev/null || break
    if curl --silent --fail --max-time 2 "$url" >/dev/null; then
      printf '%s is ready at %s\n' "$session" "$url"
      return
    fi
    sleep 1
  done
  echo "Failed to start $session. See logs/$session.log." >&2
  return 1
}

start cachet-api "$python_bin" -m uvicorn backend.app.main:app --host 127.0.0.1 --port "$api_port" --reload --reload-dir backend
start cachet-web "$python_bin" scripts/frontend_env.py npm --prefix frontend run dev -- --hostname 127.0.0.1 --port "$web_port"
wait_for_url cachet-api "http://127.0.0.1:$api_port/api/health"
wait_for_url cachet-web "http://127.0.0.1:$web_port/"
