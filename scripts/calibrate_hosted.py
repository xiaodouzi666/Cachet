"""Calibrate an operator-provisioned dedicated hosted SGLang server. Flushes that server's cache."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import manifest
from backend.app.gateway.hosted import LENGTHS, calibration_path, endpoint, metadata
from backend.app.gateway.prompt_builder import Prefix, token_hash
from backend.app.gateway.sglang_client import Engine
from backend.app.gateway.calibration import fit
from bench.ttft_bench import benchmark


def main():
    path = calibration_path()
    url, _ = endpoint()
    # Invalid input cannot generate any tokens even if the endpoint is accidentally public.
    unauth = httpx.post(
        url + "/generate",
        json={"input_ids": [], "sampling_params": {"max_new_tokens": 0}},
        timeout=8,
    )
    if unauth.status_code not in (401, 403):
        raise ValueError("Hosted inference must require a bearer token.")
    with Engine("hosted") as engine:
        identity = metadata(engine)
        prefix = Prefix(engine, "fastapi")
        hashes = {slug: token_hash(ids) for slug, ids in prefix.layers}
        bands = [benchmark(engine, n, 5) for n in LENGTHS]
        if metadata(engine) != identity:
            raise ValueError("Hosted serving identity changed during calibration.")
    if any(b["warm_p50_ms"] >= b["cold_p50_ms"] for b in bands):
        raise ValueError(
            "The hosted endpoint did not demonstrate a warm latency improvement at every band."
        )
    profile = {
        "mode": "hosted",
        "origin": url,
        "identity": identity,
        "model": manifest()["tokenizer"]["model_id"],
        "gpu": "Hosted SGLang (" + identity["device"] + ")",
        "sglang": identity["sglang"],
        "tokenizer_hashes": hashes,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "bands": bands,
        "fit": fit(bands),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(profile, indent=2) + "\n")
    temp.replace(path)
    print(f"Saved five measured hosted calibration bands to {path}")


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except (ValueError, OSError, httpx.HTTPError) as exc:
        raise SystemExit(
            f"Hosted calibration could not complete ({type(exc).__name__}): {exc}"
        )
