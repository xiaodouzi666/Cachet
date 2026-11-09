"""Corruption regressions operate on the committed native GPU measurements."""
import json

import pytest

from backend.app.replay.recordings import DIRECTORY, checked_json, layers, load, path, validate


def test_complete_file_checksum_covers_generated_text():
    trace_path = path("route-class", True)
    raw = trace_path.read_bytes()
    expected = json.loads((DIRECTORY / "sha256.json").read_bytes())[trace_path.name]
    assert checked_json(raw, expected)["turns"]
    with pytest.raises(ValueError, match="checksum"):
        checked_json(raw.replace(b'"text": "', b'"text": "altered '), expected)


def test_cumulative_input_and_span_corruption():
    record = load("rate-limit", True)
    record["turns"][1]["input_hash"] = record["turns"][0]["input_hash"]
    with pytest.raises(ValueError, match="metering"):
        validate(record, "rate-limit", True)
    record = load("rate-limit", True)
    record["turns"][0]["spans"][1]["end"] += 1
    with pytest.raises(ValueError, match="attribution"):
        validate(record, "rate-limit", True)


def test_recording_rejects_source_revision_and_changed_quote_for_same_buy_decision():
    chain = layers("rate-limit")
    record = load("rate-limit", True, source_chain=chain)
    load("rate-limit", True, source_chain=chain, request_quote=record["quote"])
    with pytest.raises(ValueError, match="context chain"):
        load("rate-limit", True, source_chain=chain[::-1])
    altered = dict(record["quote"], compute_micros=record["quote"]["compute_micros"] + 1)
    with pytest.raises(ValueError, match="quote differs"):
        load("rate-limit", True, source_chain=chain, request_quote=altered)
