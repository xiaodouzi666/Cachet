"""Keep the failed live answer intact and make its format conflict actionable."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.agents import inference
from backend.app.agents.tasks import (
    STEPS, TASKS, app_code, custom_output_format_diagnostic, turn_question,
)
from backend.app.replay.recordings import recorded_question, signature

TASK = (
    'Create a minimal Starlette ASGI app named app. GET /health must return JSON {"status":"ok"}. '
    'Keep the final response to one complete Python module, with no Markdown or explanation.'
)
FIXTURE = Path(__file__).parents[1] / "fixtures/generated_custom_fenced_response.txt"
OBSERVED = FIXTURE.read_text()


def test_actual_failed_format_is_reported_last_without_rewriting_model_output(monkeypatch):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "819d408f3124b47c152dd6bc1db5c22912424082b52e982825ed942941c81d84"
    )
    hint = custom_output_format_diagnostic(OBSERVED, TASK)
    assert "violated the requested raw Python format" in hint
    assert "first Python statement directly" in hint
    assert "Do not output opening or closing triple backticks" in hint
    prompts, stops = [], []

    def window(engine, prefix, owners, tenant, messages, buy, limit):
        prompts.append(messages[-1][1])
        return SimpleNamespace(ids=[1], spans=[])

    class Engine:
        def generate(self, ids, **kwargs):
            stops.append(kwargs["stop_at_module_fence"])
            return {"prompt_tokens": len(ids), "text": OBSERVED}

    monkeypatch.setattr(inference, "window_prompt", window)
    output = list(inference.live_turns(Engine(), None, {}, "test", TASK, True, builtin_task=False))
    assert all(r["text"] == OBSERVED for r in output if r["kind"] == "result")
    assert stops == [False] * 10
    assert prompts[9].endswith(hint)
    assert prompts[6].endswith(hint)
    assert "Python compiled successfully" in prompts[9]
    assert prompts[9].index("Python compiled successfully") < prompts[9].index(hint)


@pytest.mark.parametrize("task", [
    "Return plain Python code.",
    "Write a Python module without code fences.",
    "Write a Starlette app; do not include Markdown.",
])
def test_explicit_raw_requests_detect_actual_fences(task):
    assert "Markdown code fence" in custom_output_format_diagnostic(OBSERVED, task)


@pytest.mark.parametrize("task", [
    "Write a Python module in a fenced code block, with no explanation.",
    "Write a Markdown document explaining a Python module.",
    "Explain why the style guide says no Markdown.",
])
def test_allowed_fences_and_non_code_tasks_are_not_reinterpreted(task):
    assert custom_output_format_diagnostic(OBSERVED, task) == ""


def test_raw_code_is_not_accused_and_no_explanation_does_not_forbid_fences():
    assert custom_output_format_diagnostic(app_code(OBSERVED), TASK) == ""
    task = "Return a fenced Python module, with no explanation."
    assert custom_output_format_diagnostic(OBSERVED, task) == ""
    hint = custom_output_format_diagnostic("Here is the app:" + chr(10) + OBSERVED, task)
    assert "no-explanation requirement" in hint
    assert "raw Python format" not in hint
    assert "preserving the user's requested code-block format" in hint


def test_schema9_preserves_all_published_prompt_bytes_and_signatures():
    questions = [
        recorded_question(
            9, TASK, i, previous=OBSERVED,
            http_feedback="HTTP smoke passed: unchanged custom candidate",
        )
        for i in range(len(STEPS))
    ]
    assert hashlib.sha256(json.dumps(questions, ensure_ascii=False).encode()).hexdigest() == (
        "a3a063c285e5adcd0c75b7c09530621f4046199e4073d4c2088e6f64ae030ae0"
    )
    expected = {
        ("rate-limit", True): "fa2562bf93c1fe6bd3a0926f5ae3f83737570818a08079fe1a9a196fa9d64ee8",
        ("rate-limit", False): "f392bb46f57e589fd68cfbfb9829eed02f7313f5d6bdfed766a97899cf317ada",
        ("route-class", True): "b35ecf276c267a6fb7417c3a9259fdc63758936d21d854b0954f8cca7537cc9b",
        ("route-class", False): "fcdccd018ebba66b052d07b035fa565180577df30cb7043f05089d8007bee240",
    }
    for (task, buy), digest in expected.items():
        assert signature(task, buy, schema=9) == digest
    for schema in (6, 7, 8, 9):
        assert "Output format check" not in recorded_question(schema, TASK, 9, previous=OBSERVED)
    for _, builtin in TASKS.values():
        old = turn_question(builtin, 9, STEPS[9], previous=OBSERVED, prompt_version=9)
        assert turn_question(builtin, 9, STEPS[9], previous=OBSERVED) == old


def test_no_markdown_alone_does_not_forbid_requested_plain_explanation():
    task = "Write a Python app with no Markdown, and include a brief plain explanation."
    hint = custom_output_format_diagnostic(OBSERVED, task)
    assert "no-Markdown requirement" in hint
    assert "Retain any plain explanation the user requested" in hint
    assert "first Python statement directly" not in hint
    assert "or explanatory text" not in hint
