#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
python_bin="${PYTHON:-.venv/bin/python}"
api="${PUBLIC_API_URL:-}"
web="${PUBLIC_WEB_URL:-}"
if [[ -z "$api" || -z "$web" ]]; then
  mapfile -t configured < <("$python_bin" -c 'from backend.app.paypal.client import settings; c=settings(); print(c.get("PUBLIC_API_URL", "")); print(c.get("PUBLIC_WEB_URL", ""))')
  api="${api:-${configured[0]:-}}"
  web="${web:-${configured[1]:-}}"
fi
if [[ "$api" != https://* || "$web" != https://* ]]; then
  echo "Set PUBLIC_API_URL and PUBLIC_WEB_URL to the deployed HTTPS origins." >&2
  exit 1
fi
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
fetch_page() {
  local code
  code="$(curl --proto '=https' --max-time 25 --retry 2 --retry-delay 2 --silent --show-error \
    --output "$2" --write-out '%{http_code}' "$1")"
  if [[ "$code" != 200 ]]; then
    echo "GET $1 returned HTTP $code" >&2
    return 1
  fi
}
fetch_page "${api%/}/api/health" "$scratch/health.json"
jq -e '.service == "cachet-api" and (.version | type == "string" and length > 0)
       and .paypal_configured == true and (.mode == "live" or .mode == "replay" or .mode == "hosted")' \
  "$scratch/health.json" >/dev/null
fetch_page "${web%/}/" "$scratch/home.html"
if ! grep -Fqi 'Warm context' "$scratch/home.html" || ! grep -Fq 'href="/judge"' "$scratch/home.html"; then
  echo "Homepage loaded without the Cachet product heading or Judge Mode link." >&2
  exit 1
fi
printf 'API and homepage are available (inference: %s).\n' "$(jq -r .mode "$scratch/health.json")"
if [[ "${1:-}" == --health-only ]]; then
  exit 0
fi
"$python_bin" scripts/e2e_demo.py --smoke --api "$api" "$@"
