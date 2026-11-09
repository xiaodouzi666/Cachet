"""Real native transport checks. The primary service supplies contract evidence only."""

import secrets

import pytest

from backend.app.config import ROOT
from backend.app.gateway.hosted import endpoint, read_profile


def test_primary_calibration_cannot_enable_hosted(monkeypatch):
    monkeypatch.setenv(
        "HOSTED_FALLBACK_CALIBRATION", str(ROOT / "bench/results/calibration.json")
    )
    with pytest.raises(ValueError, match="different origin or model"):
        read_profile()


def test_rejects_reusing_primary_as_hosted(monkeypatch):
    primary = "https://primary.example.invalid"
    monkeypatch.setenv("SGLANG_BASE_URL", primary)
    monkeypatch.setenv("HOSTED_FALLBACK_BASE_URL", primary)
    monkeypatch.setenv("HOSTED_FALLBACK_API_KEY", secrets.token_urlsafe(16))
    with pytest.raises(ValueError, match="distinct"):
        endpoint()


def test_native_transport_returns_per_request_cache_counts(engine):
    # No flush: isolate this real prompt so other concurrent sessions retain their prefixes.
    ids = engine.encode(
        secrets.token_hex(16) + "\nDescribe cache reuse in a sentence. " * 12
    )
    first = engine.generate(ids, 4)
    second = engine.generate(ids, 4)
    assert first["prompt_tokens"] == second["prompt_tokens"] == len(ids)
    assert 0 <= first["cached_tokens"] < second["cached_tokens"] <= len(ids)
    assert second["output_tokens"] > 0 and second["finish_reason"] in {"stop", "length"}
