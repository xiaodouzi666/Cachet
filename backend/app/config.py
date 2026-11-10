import json
import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def value(name, default=""):
    return os.getenv(name) or dotenv_values(ROOT / ".env").get(name) or default


@lru_cache
def manifest():
    return json.loads((ROOT / "contexts/manifest.json").read_text())


def sources():
    return {entry["slug"]: entry for entry in manifest()["contexts"]}


def calibration(mode="live"):
    if mode == "replay":
        # Archived model explanations and SLA receipts use the benchmark from their recording.
        return json.loads(Path(__file__).with_name("replay").joinpath("calibration.json").read_text())
    if mode == "hosted":
        from .gateway.hosted import read_profile
        return read_profile()
    profile = json.loads((ROOT / "bench/results/calibration.json").read_text())
    if mode == "live" and value("INFERENCE_ENDPOINT_MODE", "direct") == "registered":
        from .gateway.route_calibration import apply, current
        return apply(profile, current())
    return profile


def band_for(tokens, profile=None):
    return min((profile or calibration())["bands"], key=lambda b: abs(b["prefix_tokens"] - tokens))
