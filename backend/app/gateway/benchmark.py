"""Measure a cold and warm pair through the active native transport."""
import json
from statistics import median

from backend.app.config import ROOT


def benchmark(engine, length=26_000, trials=3):
    manifest = json.loads((ROOT / "contexts/manifest.json").read_text())
    prefix = engine.encode("<|im_start|>system\nYou are a coding assistant. Repository context follows.\n")
    for layer in manifest["contexts"]:
        prefix.extend(engine.encode((ROOT / layer["text_path"]).read_text()))
    # Fill to precisely 26k so different selections of source files remain comparable.
    filler = engine.encode("\n# End of repository context.\n")
    prefix.extend(filler * (max(0, length - len(prefix)) // len(filler) + 1))
    prompt = prefix[:length] + engine.encode(
        "<|im_end|>\n<|im_start|>user\nName the two Python frameworks above."
        "<|im_end|>\n<|im_start|>assistant\n"
    )
    runs = []
    for _ in range(trials):
        engine.flush()
        cold = engine.timed_sample(prompt)
        warm = engine.timed_sample(prompt)
        if cold["prompt_tokens"] != len(prompt) or warm["prompt_tokens"] != len(prompt):
            raise ValueError("SGLang reported an unexpected prompt length")
        if cold["cached_tokens"] != 0 or warm["cached_tokens"] < 0.95 * length:
            raise ValueError(f"Invalid cache state in trial: {cold}, {warm}")
        runs.append({"cold": cold, "warm": warm})
    return {
        "prefix_tokens": length, "prompt_tokens": len(prompt), "runs": runs,
        "cold_p50_ms": median(pair["cold"]["ttft_ms"] for pair in runs),
        "warm_p50_ms": median(pair["warm"]["ttft_ms"] for pair in runs),
    }
