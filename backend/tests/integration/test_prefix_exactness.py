import json
import re
from itertools import takewhile

import pytest


@pytest.mark.gpu
@pytest.mark.parametrize("tenants", [("3a1b5f07", "9ce48062"), ("9ce48062", "3a1b5f07")])
def test_exact_segments_and_private_suffix(engine, tenants):
    system = "<|im_start|>system\nYou are a coding assistant. Repository context follows.\n"
    framework = "# routing.py\ndef route(path):\n    return path\n" * 200
    extension = "# app.py\ndef app():\n    return route('/')\n" * 200
    # Align the public boundary so a whole-page engine can reuse every public token.
    page_size = engine.get("/server_info").raise_for_status().json()["page_size"]
    assert page_size > 0
    earlier = engine.encode(system) + engine.encode(framework)
    padding = 1 + (-(len(earlier) + len(engine.encode(extension + "# x\n"))) % page_size)
    extension += "#" + " x" * padding + "\n"
    public_ids = earlier + engine.encode(extension)
    assert len(public_ids) % page_size == 0

    private = (
        "<|im_end|>\n<|im_start|>user\n"
        + "# local test notes\n" * 160
        + "Answer with one word. What HTTP method retrieves a resource?<|im_end|>\n<|im_start|>assistant\n"
    )
    salts = [f"\n<!-- tenant:{tenant} -->\n" for tenant in tenants]
    salt_ids = [engine.encode(salt) for salt in salts]
    private_ids = engine.encode(private)
    prompts = [public_ids + salt + private_ids for salt in salt_ids]
    for ids, salt in zip(prompts, salts):
        assert engine.decode(ids) == system + framework + extension + salt + private

    engine.flush()
    samples = [engine.sample(prompts[i]) for i in (0, 0, 1, 1)]
    cold_a, warm_a, cross_b, warm_b = [r["meta_info"]["cached_tokens"] for r in samples]
    shared_salt = sum(1 for _ in takewhile(lambda pair: pair[0] == pair[1], zip(*salt_ids)))
    assert cold_a == 0
    assert warm_a > len(public_ids) + len(salt_ids[0])
    assert warm_b > len(public_ids) + len(salt_ids[1])
    assert len(public_ids) <= cross_b <= len(public_ids) + shared_salt
    assert all(re.search(r"\bGET\b", r["text"], re.IGNORECASE) for r in samples)
    engine.flush()
    after_flush = engine.sample(prompts[1])["meta_info"]["cached_tokens"]
    assert after_flush == 0
    print(json.dumps({"seed_tenant": tenants[0], "page_size": page_size, "public": len(public_ids), "shared_salt": shared_salt,
                      "cold_a": cold_a, "warm_a": warm_a, "cross_b": cross_b,
                      "warm_b": warm_b, "after_flush": after_flush}))
