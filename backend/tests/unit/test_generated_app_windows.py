"""Regressions from a real live answer that counted rejected requests as quota."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from backend.app.agents import inference
from backend.app.agents.tasks import STEPS, TASKS, app_code, turn_question
from backend.app.replay.recordings import load, recorded_question
from scripts.check_generated_apps import IMAGE, proposal_feedback, run_http

FIXTURE = Path(__file__).parents[1] / "fixtures/generated_rate_limit_rejected_attempts.py"
BAD_ANSWER = chr(96) * 3 + "python" + chr(10) + FIXTURE.read_text() + chr(96) * 3
TIMELINE = "0, 1, 30, 40, 62"


@pytest.fixture(scope="module")
def docker_verifier():
    if not shutil.which("docker"):
        pytest.skip("Docker is required for isolated generated-app HTTP checks")
    available = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=10
    )
    if available.returncode:
        pytest.skip("Build scripts/generated_app.Dockerfile to run HTTP checks")


def test_observed_rejected_attempts_fail_real_http_and_return_repair_feedback(docker_verifier):
    # This is the exact module displayed by the fresh browser run; its source is unchanged.
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "a76797593783f7e5d352e558bede4abf9495cc342f0294bd071b072c220138c9"
    )
    with pytest.raises(ValueError, match="Rejected requests must not consume quota"):
        run_http("rate-limit", BAD_ANSWER, full=False)
    feedback = proposal_feedback("rate-limit", BAD_ANSWER)
    assert feedback.startswith("HTTP smoke failed.")
    assert "observed [200, 200, 429, 429, 429]" in feedback
    repaired_prompt = turn_question(
        TASKS["rate-limit"][1], 9, STEPS[9],
        previous=BAD_ANSWER, http_feedback=feedback,
    )
    assert "Repair the actual HTTP failure above" in repaired_prompt
    assert "Repeat the preceding working module verbatim" not in repaired_prompt


@pytest.mark.parametrize("buy", [True, False])
def test_valid_archived_modules_pass_the_same_real_http_boundary(docker_verifier, buy):
    answer = load("rate-limit", buy)["turns"][-1]["text"]
    receipt = run_http("rate-limit", answer, full=False)
    assert receipt["source_sha256"] == hashlib.sha256(
        app_code(answer, allow_prose=True).encode()
    ).hexdigest()
    assert receipt["http"] == {"status_codes": [200, 200, 429, 200], "distinct_clients": 2}
    review = turn_question(TASKS["rate-limit"][1], 9, STEPS[9], previous=answer)
    assert "Append before quota rejection" not in review


@pytest.mark.parametrize("alias", ["time", "clock"])
def test_valid_module_import_clock_preserves_other_time_attributes(docker_verifier, alias):
    source = app_code(load("rate-limit", False)["turns"][-1]["text"], allow_prose=True)
    source = source.replace("from time import monotonic", f"import time as {alias}")
    source = source.replace(
        "now = monotonic()",
        f"now = {alias}.monotonic()" + chr(10) + f"    assert {alias}.get_clock_info('monotonic').monotonic",
    )
    answer = chr(96) * 3 + "python" + chr(10) + source + chr(96) * 3
    assert run_http("rate-limit", answer, full=False)["http"]["status_codes"] == [
        200, 200, 429, 200,
    ]


@pytest.mark.parametrize("buy", [True, False])
def test_live_prompt_and_static_repair_apply_without_a_docker_callback(monkeypatch, buy):
    prompts = []

    def window(engine, prefix, owners, tenant, messages, selected, limit):
        prompts.append((selected, messages[-1][1]))
        return SimpleNamespace(ids=[1], spans=[])

    class Engine:
        def generate(self, ids, **kwargs):
            return {"prompt_tokens": len(ids), "text": BAD_ANSWER}

    monkeypatch.setattr(inference, "window_prompt", window)
    result = list(inference.live_turns(
        Engine(), None, {}, "test", TASKS["rate-limit"][1], buy
    ))
    assert len([r for r in result if r["kind"] == "result"]) == 10
    assert all(selected is buy and TIMELINE in prompt for selected, prompt in prompts)
    assert "Append before quota rejection" in prompts[6][1]
    assert "Independent sandbox HTTP check" not in prompts[6][1]
    for schema in (6, 7, 8):
        old = recorded_question(
            schema, TASKS["rate-limit"][1], 6, previous=BAD_ANSWER
        )
        assert TIMELINE not in old and "Append before quota rejection" not in old


def test_existing_archive_prompt_sequences_keep_their_original_fingerprints():
    # Captured from a9953e95dcf0 before this fix, including prior answers and HTTP feedback.
    expected = {
        ("rate-limit", True): "052d4f30d7abe3d2db7be5594c664aaea31a639c2fc70b3c5135303a81fc11a4",
        ("rate-limit", False): "ef46d6b947686a09821d4abe22c61c13a815665f544a8d364a1c7b17981f9a50",
        ("route-class", True): "5f69ab088954995231df88726da8c05138af5dfcd17790b93a7aeb67ebff8a2f",
        ("route-class", False): "6e429e4ca440cb4039ce1f09d703dc415b8d082f3c46f477174a4c8412a9ab0f",
    }
    for (task_id, buy), fingerprint in expected.items():
        record = load(task_id, buy)
        task = TASKS[task_id][1]
        questions = [
            recorded_question(
                record["schema"], task, i, quote=record["quote"],
                previous=record["turns"][i - 1]["text"] if i else "",
                http_feedback=record["turns"][i - 1].get("http_feedback") if i else None,
            )
            for i in range(len(record["turns"]))
        ]
        assert hashlib.sha256(json.dumps(questions, ensure_ascii=False).encode()).hexdigest() == fingerprint


def test_custom_live_task_keeps_user_format_in_proposal_revision_and_final(monkeypatch):
    task = (
        "Create a minimal Starlette app with GET /health returning "
        "JSON {'service': 'cachet', 'ready': True}. "
        "Final response: one complete Python module, no Markdown or explanation."
    )
    prompts, stop_flags = [], []

    def window(engine, prefix, owners, tenant, messages, buy, limit):
        prompts.append(messages[-1][1])
        return SimpleNamespace(ids=[1], spans=[])

    class Engine:
        def generate(self, ids, **kwargs):
            stop_flags.append(kwargs["stop_at_module_fence"])
            return {"prompt_tokens": len(ids), "text": "A test response"}

    monkeypatch.setattr(inference, "window_prompt", window)
    list(inference.live_turns(Engine(), None, {}, "test", task, False, builtin_task=False))
    assert stop_flags == [False] * 10
    for step in (5, 8, 9):
        assert task in prompts[step]
        assert "Respect the user's requested output format" in prompts[step]
        assert "Do not add Markdown fences" in prompts[step]
        assert "fenced Python" not in prompts[step]
        assert "one fenced python" not in prompts[step]
    final_with_feedback = turn_question(
        task, 9, STEPS[9], http_feedback="HTTP smoke passed: user artifact check"
    )
    assert "single fenced python block" not in final_with_feedback
    assert "Respect the user's requested output format" in final_with_feedback
    # Legacy archived instructions stay reconstructible, including their former code fences.
    assert "one fenced Python code block" in turn_question(task, 5, STEPS[5], prompt_version=8)
    assert "one fenced Python block" in turn_question(task, 8, STEPS[8], prompt_version=8)
    for _, builtin in TASKS.values():
        assert "exactly one fenced python code block" in turn_question(builtin, 9, STEPS[9])
