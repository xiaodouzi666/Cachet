"""Exercise a physical disconnect of the GPU agent's tunnel, then restore it."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

import httpx
from gpu_agent import EDGE, PAUSE, ROOT, value


def wait_mode(client, wanted, seconds):
    start = time.monotonic()
    samples = []
    while time.monotonic() - start < seconds:
        try:
            response = client.get("/api/health", timeout=6)
            state = response.json() if response.is_success else {}
            mode = state.get("mode", "unavailable")
        except (httpx.HTTPError, ValueError):
            mode = "unavailable"
        samples.append({"t": round(time.monotonic()-start, 3),"mode":mode})
        if mode == wanted:
            return samples
        time.sleep(1)
    raise RuntimeError(f"The API did not reach {wanted} within {seconds}s; observed {samples[-1:]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=value("PUBLIC_API_URL"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.api or not EDGE.is_file() or PAUSE.exists():
        raise SystemExit("Supply the backend origin and start the GPU agent first.")
    evidence = {"observed_utc":datetime.now(timezone.utc).isoformat()}
    api = urlsplit(args.api)
    local = api.hostname in {"127.0.0.1", "localhost"}
    if not local and (api.scheme != "https" or args.api.rstrip("/") != value("PUBLIC_API_URL").rstrip("/")):
        raise SystemExit("Select the configured production origin or a local integration API")
    token = value("ADMIN_TOKEN") if local else (value("PRODUCTION_ADMIN_TOKEN") or value("ADMIN_TOKEN"))
    headers = {"X-Admin-Token":token}
    with httpx.Client(base_url=args.api.rstrip("/"), follow_redirects=False) as client:
        evidence["initial"] = wait_mode(client, "live", 30)
        old_url = json.loads(EDGE.read_text())["url"]
        registered = client.get("/api/admin/inference-endpoint",headers=headers).raise_for_status().json()
        if not registered["live"] or registered["url"] != old_url:
            raise RuntimeError("Selected backend is not using this GPU tunnel")
        subprocess.run([sys.executable, str(ROOT/"scripts/gpu_agent.py"), "pause"],check=True)
        try:
            evidence["disconnect"] = wait_mode(client, "replay", 90)
            if EDGE.exists():
                raise RuntimeError("The test tunnel has not physically stopped")
        finally:
            subprocess.run([sys.executable, str(ROOT/"scripts/gpu_agent.py"), "resume"],check=True)
        evidence["reconnect"] = wait_mode(client, "live", 60)
        evidence["origin_changed"] = old_url != json.loads(EDGE.read_text())["url"]
        registered = client.get("/api/admin/inference-endpoint",headers=headers).raise_for_status().json()
        if not registered["live"] or registered["url"] != json.loads(EDGE.read_text())["url"]:
            raise RuntimeError("The backend did not adopt the restored origin")
        evidence["heartbeat_matches_restored_origin"] = True
        evidence["final"] = {k:v for k,v in client.get("/api/health").raise_for_status().json().items()
                             if k in {"build","mode","model","gpu","sglang_version"}}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(evidence,indent=2)+"\n")
    print("Disconnect and reconnect seconds:", evidence["disconnect"][-1]["t"], evidence["reconnect"][-1]["t"])


if __name__ == "__main__":
    main()
