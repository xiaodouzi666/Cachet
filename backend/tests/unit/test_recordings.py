"""Corruption regressions operate on the committed native GPU measurements."""

import json

import pytest

from backend.app.replay.recordings import (
    DIRECTORY,
    checked_json,
    layers,
    load,
    path,
    validate,
)


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
    altered = dict(
        record["quote"], compute_micros=record["quote"]["compute_micros"] + 1
    )
    with pytest.raises(ValueError, match="quote differs"):
        load("rate-limit", True, source_chain=chain, request_quote=altered)


def test_recorded_terminal_and_public_reference_expose_only_the_selected_application_evidence():
    from backend.app.api.agent import archived_reference_turn
    from backend.app.replay.recordings import application_evidence

    for task in ("rate-limit", "route-class"):
        for buy in (True, False):
            decision = "buy" if buy else "compute"
            track = load(task, buy)
            reference = archived_reference_turn(task, decision)
            assert reference["decision"] == decision and reference["final_turn"] == len(
                track["turns"]
            )
            assert reference["final_output_text"] == track["turns"][-1]["text"]
            assert reference["application_check"] == track["application_check"]
            assert (
                reference["verified_application_turn"]
                == track["verified_application_turn"]
            )
            assert application_evidence(track)["application_check"]["status"] in {
                "passed",
                "failed",
            }
    live = application_evidence()
    assert (
        live["application_check"]["status"] == "unverified"
        and live["verified_application_turn"] is None
    )


def test_compiler_and_previous_sandbox_feedback_are_in_subsequent_prompt():
    from backend.app.agents.tasks import STEPS, TASKS, turn_question

    task = TASKS["rate-limit"][1]
    # Python parser test vector, not a claimed execution of a generated app.
    malformed = "```python\nif:\n    pass\n```"
    prompt = turn_question(task, 6, STEPS[6], previous=malformed)
    assert "SyntaxError at line 1" in prompt
    track = load("route-class", True)
    proposal = track["turns"][8]
    next_prompt = turn_question(
        TASKS["route-class"][1],
        9,
        STEPS[9],
        previous=proposal["text"],
        http_feedback=proposal["http_feedback"],
    )
    assert proposal["http_feedback"] in next_prompt


def test_fastapi_review_uses_the_contract_in_the_existing_dialogue():
    from backend.app.agents.tasks import CONTRACTS, STEPS, TASKS, turn_question

    task = TASKS["rate-limit"][1]
    questions = [turn_question(task, i, step) for i, step in enumerate(STEPS)]
    compact_turns = [i + 1 for i, q in enumerate(questions) if CONTRACTS[task] not in q]
    assert compact_turns == [7, 8]
    assert all("200, 200, 429" in questions[i - 1] for i in compact_turns)
