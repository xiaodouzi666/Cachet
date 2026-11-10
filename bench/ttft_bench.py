"""Measure client-observed cold and warm time to first decoded text."""

import json
import sys
from pathlib import Path

from backend.app.gateway.sglang_client import Engine
from backend.app.gateway.benchmark import benchmark

ROOT = Path(__file__).resolve().parents[1]
PREFIX_TOKENS = 26_000
TRIALS = 3


def main():
    with Engine() as engine:
        result = benchmark(engine)
    print(json.dumps(result, indent=2))
    if result["cold_p50_ms"] <= result["warm_p50_ms"]:
        print("The warm median did not improve over the cold median.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
