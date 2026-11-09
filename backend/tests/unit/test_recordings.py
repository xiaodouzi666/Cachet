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


def test_recording_version_selects_original_or_current_coding_prompt():
    from backend.app.agents.tasks import STEPS, TASKS, turn_question
    from backend.app.replay.recordings import SCHEMA, recorded_question, signature

    # Preserve the original specifications even after a new GPU run replaces a trace.
    schema_6_signatures = {
        ("rate-limit", True): "0e89e7f9624f579cee41ed07bb48e1dc2f48042bbcbc2fe6753e83c72ec64bdc",
        ("rate-limit", False): "611f26f6b6912ed2298ca98d28bfc7e8a4a94ceaed59a6d475e093dc5d7026c1",
        ("route-class", True): "f547a8bc5841c6529f65539fc19c18cd3854c16be459757da5779516f83aaf5e",
        ("route-class", False): "a35f2928568926141f195cdef20cc9d5bfeab03dfc6066c92a14068dc228c480",
    }
    for task_id, (_, task) in TASKS.items():
        planning = turn_question(task, 0, STEPS[0])
        assert "implementation steps" in planning and "Quote:" not in planning
        for buy in (True, False):
            record = load(task_id, buy)
            quote = record["quote"]
            current = recorded_question(SCHEMA, task, 0, quote=quote)
            assert current == planning
            legacy = recorded_question(6, task, 0, quote=quote)
            assert "two quoted costs in microdollars" in legacy
            assert legacy.endswith("\nQuote: " + json.dumps(quote, sort_keys=True))
            assert signature(task_id, buy, schema=6) == schema_6_signatures[task_id, buy]
            assert signature(task_id, buy) != schema_6_signatures[task_id, buy]
            assert record["signature"] == signature(task_id, buy, schema=record["schema"])
            wrong_schema = 6 if record["schema"] == SCHEMA else SCHEMA
            with pytest.raises(ValueError, match="specification"):
                validate({**record, "schema": wrong_schema}, task_id, buy)


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


def test_native_stop_strings_apply_only_to_builtin_single_module_turns():
    from backend.app.agents.tasks import STEPS, TASKS, requests_module
    from backend.app.gateway.sglang_client import sampling_params

    normal = {"temperature": 0, "max_new_tokens": 640}
    assert sampling_params(640) == normal
    assert sampling_params(640, stop_at_module_fence=True) == {
        **normal, "stop": ["\n```\n", "\r\n```\r\n"], "no_stop_trim": True,
    }
    for _, task in TASKS.values():
        assert [i for i in range(len(STEPS)) if requests_module(task, i)] == [5, 8, 9]
        assert not any(requests_module(task, i, builtin_task=False) for i in range(len(STEPS)))
    assert not any(requests_module("An independent task", i) for i in range(len(STEPS)))


def test_starlette_review_diagnoses_raw_headers_in_the_mapping_parameter():
    from backend.app.agents.tasks import STEPS, TASKS, turn_question

    def review(expression):
        previous = "\n".join((
            "```python",
            "from starlette.datastructures import MutableHeaders",
            "def customize(message):",
            f"    headers = MutableHeaders({expression})",
            "    headers['X-Route-Class'] = 'custom'",
            "```",
        ))
        return turn_question(TASKS["route-class"][1], 6, STEPS[6], previous=previous)

    # The original failure compiled successfully. Diagnose it before a new HTTP check.
    for expression in ("message.get('headers', [])", "message['headers']"):
        prompt = review(expression)
        assert "Line 3: MutableHeaders(" in prompt
        assert "Replace that constructor call with MutableHeaders(scope=message)" in prompt
    for expression in ("scope=message", "raw=message['headers']", "{'x': 'y'}"):
        assert "Replace that constructor call" not in review(expression)
