"""Measure client-observed cold and warm time to first decoded text."""

import json
import sys
from pathlib import Path
from statistics import median

from backend.app.gateway.sglang_client import Engine

ROOT = Path(__file__).resolve().parents[1]
PREFIX_TOKENS = 26_000
TRIALS = 3


def benchmark(engine):
    manifest = json.loads((ROOT / "contexts/manifest.json").read_text())
    prefix = engine.encode("<|im_start|>system\nYou are a coding assistant. Repository context follows.\n")
    for layer in manifest["contexts"]:
        prefix.extend(engine.encode((ROOT / layer["text_path"]).read_text()))
    # Fill to precisely 26k so different selections of source files remain comparable.
    filler = engine.encode("\n# End of repository context.\n")
    prefix.extend(filler * (max(0, PREFIX_TOKENS - len(prefix)) // len(filler) + 1))
    prompt = prefix[:PREFIX_TOKENS] + engine.encode(
        "<|im_end|>\n<|im_start|>user\nName the two Python frameworks above."
        "<|im_end|>\n<|im_start|>assistant\n"
    )
    runs = []
    for _ in range(TRIALS):
        engine.flush()
        cold = engine.timed_sample(prompt)
        warm = engine.timed_sample(prompt)
        if cold["prompt_tokens"] != len(prompt) or warm["prompt_tokens"] != len(prompt):
            raise ValueError("SGLang reported an unexpected prompt length")
        if cold["cached_tokens"] != 0 or warm["cached_tokens"] < 0.95 * PREFIX_TOKENS:
            raise ValueError(f"Invalid cache state in trial: {cold}, {warm}")
        runs.append({"cold": cold, "warm": warm})
    return {
        "prefix_tokens": PREFIX_TOKENS, "prompt_tokens": len(prompt), "runs": runs,
        "cold_p50_ms": median(pair["cold"]["ttft_ms"] for pair in runs),
        "warm_p50_ms": median(pair["warm"]["ttft_ms"] for pair in runs),
    }


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
