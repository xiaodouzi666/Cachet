"""Real quote recordings and deliberate corruption checks for their verification."""

from copy import deepcopy

import pytest

from backend.app.agents.quote_reason import (
    canonical_quote,
    recorded_reason,
    validate_evidence,
    validate_reason,
)
from backend.app.agents.tasks import TASKS
from backend.app.replay.recordings import load


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize("buy", (True, False))
def test_recorded_quote_explanation(task, buy):
    quote = load(task, buy)["quote"]
    receipt = recorded_reason(quote)
    assert receipt["source"] == "recorded" and receipt["quote"] == quote
    validate_evidence(receipt, quote)
    with pytest.raises(ValueError, match="exact recorded quote"):
        recorded_reason({**quote, "warm_ttft_ms": quote["warm_ttft_ms"] + 1})


def test_corrupted_explanation_or_input_is_rejected():
    quote = load("rate-limit", True)["quote"]
    receipt = recorded_reason(quote)
    changed = deepcopy(receipt)
    changed["input_ids"][-1] += 1
    with pytest.raises(ValueError, match="hash"):
        validate_evidence(changed, quote)
    wrong_price = receipt["reason"].replace(
        str(quote["buy_micros"]), str(quote["compute_micros"])
    )
    with pytest.raises(ValueError, match="quoted price"):
        validate_reason(wrong_price, quote)
    with pytest.raises(ValueError, match="quoted price"):
        validate_reason(receipt["reason"].replace("I choose", "I do not choose"), quote)
    assert (
        canonical_quote({**quote, "context_ids": ["sensitive-workspace-context"]})
        == quote
    )


@pytest.mark.parametrize(
    "key,invalid",
    [
        ("ttft_ms", True),
        ("prompt_tokens", False),
        ("output_tokens", True),
        ("total_ms", float("inf")),
        ("generated_at", "2026-11-09"),
        ("retry", 1),
    ],
)
def test_invalid_receipt_metadata_is_rejected(key, invalid):
    quote = load("route-class", False)["quote"]
    receipt = {**recorded_reason(quote), key: invalid}
    with pytest.raises(ValueError):
        validate_evidence(receipt, quote)
