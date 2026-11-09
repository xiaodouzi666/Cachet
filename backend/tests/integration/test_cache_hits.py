import json

import pytest


@pytest.mark.gpu
def test_repeated_12k_prefix(engine):
    text = "def sample(value):\n    return value + 1\n"
    ids = engine.encode(text * 2400)[:12000]
    assert len(ids) == 12000
    engine.flush()
    cold = engine.sample(ids)["meta_info"]
    warm = engine.sample(ids)["meta_info"]
    for meta in (cold, warm):
        assert meta["prompt_tokens"] == len(ids)
    print(json.dumps({"cold": cold["cached_tokens"], "warm": warm["cached_tokens"],
                      "cached_tokens_details": warm.get("cached_tokens_details")}))
    assert cold["cached_tokens"] == 0
    assert warm["cached_tokens"] >= len(ids) * .95
    details = warm.get("cached_tokens_details")
    if details is not None:
        assert details["device"] + details["host"] + (details.get("storage") or 0) == warm["cached_tokens"]
