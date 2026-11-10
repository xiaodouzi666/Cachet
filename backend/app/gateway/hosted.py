"""Eligibility and calibration of an independently hosted, native SGLang deployment."""

import hashlib
import ipaddress
import json
import math
import socket
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit

from backend.app.config import ROOT, manifest, value

LENGTHS = (2000, 4000, 8000, 16000, 26000)


def endpoint():
    url = value("HOSTED_FALLBACK_BASE_URL").rstrip("/")
    key = value("HOSTED_FALLBACK_API_KEY")
    host = urlsplit(url)
    primary = urlsplit(value("SGLANG_BASE_URL", "http://127.0.0.1:30000"))
    if (
        not key
        or host.scheme != "https"
        or not host.hostname
        or host.username
        or host.password
        or host.query
        or host.fragment
        or host.path
        or host.hostname.rstrip(".").lower() == "localhost"
        or host.hostname.rstrip(".").lower()
        == (primary.hostname or "").rstrip(".").lower()
    ):
        raise ValueError(
            "Configure a distinct, authenticated HTTPS origin for native hosted SGLang."
        )
    addresses = socket.getaddrinfo(
        host.hostname, host.port or 443, type=socket.SOCK_STREAM
    )
    if not addresses or any(
        not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses
    ):
        raise ValueError("Hosted SGLang must use an external origin.")
    return url, key


def metadata(engine):
    """Only retain whitelisted identity fields. SGLang server_info contains sensitive configuration."""
    model = engine.get("/model_info", timeout=4).raise_for_status().json()
    server = engine.get("/server_info", timeout=4).raise_for_status().json()
    pin = manifest()["tokenizer"]
    if (
        model["served_model_name"] != pin["model_id"]
        or model["model_path"] != pin["model_id"]
        or model["tokenizer_path"] != pin["model_id"]
        or server.get("revision") != pin["revision"]
        or not model.get("is_generation")
        or server.get("disable_radix_cache")
        or int(server["context_length"]) < manifest()["context_window"]
    ):
        raise ValueError(
            "Hosted SGLang does not report the pinned Qwen model, revision, tokenizer and context window."
        )
    if value("HOSTED_FALLBACK_MODEL", pin["model_id"]) != pin["model_id"]:
        raise ValueError(
            "The hosted model must match the licensed pinned source manifest."
        )
    return {
        "model": model["served_model_name"],
        "revision": server["revision"],
        "tokenizer": model["tokenizer_path"],
        "sglang": server["version"],
        "device": server["device"],
        "tp_size": server["tp_size"],
        "dtype": server.get("dtype"),
        "quantization": server.get("quantization"),
        "pp_size": server.get("pp_size"),
        "context_length": server["context_length"],
        "weight_version": model.get("weight_version"),
    }


def calibration_path():
    path = value("HOSTED_FALLBACK_CALIBRATION")
    if not path:
        raise ValueError(
            "Configure an independently measured HOSTED_FALLBACK_CALIBRATION file."
        )
    return ROOT / Path(path)


def fingerprint(profile):
    return hashlib.sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_profile():
    from backend.app.gateway.calibration import fit

    profile = json.loads(calibration_path().read_bytes())
    pin = manifest()["tokenizer"]
    if (
        profile.get("mode") != "hosted"
        or profile["origin"] != value("HOSTED_FALLBACK_BASE_URL").rstrip("/")
        or profile["model"] != pin["model_id"]
        or profile["identity"]["revision"] != pin["revision"]
        or profile["tokenizer_hashes"]
        != {r["slug"]: r["ids_sha256"] for r in manifest()["contexts"]}
    ):
        raise ValueError("Hosted calibration belongs to a different origin or model.")
    bands = profile["bands"]
    if tuple(b["prefix_tokens"] for b in bands) != LENGTHS or len(bands) != len(
        LENGTHS
    ):
        raise ValueError("Incomplete hosted SLA calibration.")
    for b in bands:
        rows = b["runs"]
        n = b["prefix_tokens"]
        if len(rows) != 5:
            raise ValueError(
                "Each hosted band requires five fresh cold/warm measurements."
            )
        for r in rows:
            for side in ("cold", "warm"):
                sample = r[side]
                if (
                    sample["prompt_tokens"] != b["prompt_tokens"]
                    or sample["prompt_tokens"] < n
                    or type(sample["cached_tokens"]) is not int
                    or not 0 <= sample["cached_tokens"] <= b["prompt_tokens"]
                    or not math.isfinite(sample["ttft_ms"])
                    or sample["ttft_ms"] <= 0
                ):
                    raise ValueError("Invalid raw hosted benchmark receipt.")
            if r["cold"]["cached_tokens"] != 0 or r["warm"]["cached_tokens"] < 0.95 * n:
                raise ValueError(
                    "Hosted cache counts did not verify cold and warm states."
                )
        for side in ("cold", "warm"):
            if median(r[side]["ttft_ms"] for r in rows) != b[side + "_p50_ms"]:
                raise ValueError("Hosted median differs from its observations.")
        if b["warm_p50_ms"] >= b["cold_p50_ms"]:
            raise ValueError("Hosted warm median did not improve.")
    if profile["fit"] != fit(bands):
        raise ValueError("Hosted prefill fit differs from its observations.")
    return profile


def checked_profile(engine):
    p = read_profile()
    if metadata(engine) != p["identity"]:
        raise ValueError("Hosted deployment changed since calibration.")
    return p
