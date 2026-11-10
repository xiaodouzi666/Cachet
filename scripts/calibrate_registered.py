"""Commission the live API's measured SLA profile and save the five raw pairs per band."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.config import value
from backend.app.gateway.route_calibration import complete_bands


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api", default=value("PUBLIC_API_URL"))
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    base = args.api.rstrip("/")
    origin = urlsplit(base)
    local = origin.hostname in {"localhost", "127.0.0.1"}
    if not local and (origin.scheme != "https" or base != value("PUBLIC_API_URL").rstrip("/")):
        p.error("Select the configured production HTTPS origin or the local test API")
    key = value("ADMIN_TOKEN") if local else (value("PRODUCTION_ADMIN_TOKEN") or value("ADMIN_TOKEN"))
    if not key:
        p.error("The backend operator credential is required")
    os.umask(0o077)
    route = "/api/admin/inference-calibration"
    with httpx.Client(base_url=base, headers={"X-Admin-Token":key}, timeout=25, follow_redirects=False) as client:
        started = client.post(route)
        if started.status_code not in (200, 202):
            raise SystemExit(f"Transport calibration did not start (HTTP {started.status_code})")
        previous = -1
        until = time.monotonic()+900
        while time.monotonic() < until:
            try:
                result = client.get(route).raise_for_status().json()
            except httpx.TransportError:
                time.sleep(4)
                continue
            count = len(result.get("bands", []))
            if count != previous:
                print(f"Measured {count}/5 latency bands.",flush=True)
                previous = count
            if result.get("status") == "failed":
                raise SystemExit("Transport calibration failed: "+result.get("error","unknown"))
            if result.get("status") == "completed":
                if not complete_bands(result.get("bands",[])):
                    raise SystemExit("Incomplete or inconsistent transport samples")
                args.output.parent.mkdir(parents=True,exist_ok=True)
                temp = args.output.with_suffix(".tmp")
                temp.write_text(json.dumps(result,indent=2)+"\n")
                os.replace(temp,args.output)
                print("Five bands, 25 verified cold/warm pairs.",flush=True)
                return
            time.sleep(3)
    raise SystemExit("Calibration is still incomplete; read the existing job before retrying.")


if __name__ == "__main__":
    main()
