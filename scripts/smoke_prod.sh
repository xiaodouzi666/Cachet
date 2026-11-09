#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
python_bin="${PYTHON:-.venv/bin/python}"
# Require a configured deployment origin. Never quietly test localhost as a deployment.
api="$("$python_bin" -c 'from backend.app.paypal.client import settings; print(settings().get("PUBLIC_API_URL", ""))')"
case "$api" in https://*) ;; *) echo "PUBLIC_API_URL must be the deployed HTTPS backend origin." >&2; exit 1 ;; esac
exec "$python_bin" scripts/e2e_demo.py --smoke --api "$api" "$@"
