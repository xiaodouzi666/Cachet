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


def calibration():
    return json.loads((ROOT / "bench/results/calibration.json").read_text())


def band_for(tokens):
    return min(calibration()["bands"], key=lambda b: abs(b["prefix_tokens"] - tokens))
