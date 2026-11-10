import pytest

from backend.app.agents.window import window_prompt
from backend.app.gateway.prompt_builder import Prefix
from backend.app.replay.recordings import load


class Utf8Tokenizer:
    """A deterministic byte tokenizer for exercising exact boundary lengths on CPU."""
    @staticmethod
    def encode(text):
        return list(text.encode("utf-8"))


def test_window_bounds_and_keeps_current_request_and_recent_exchanges():
    # Use the production Prefix.build and real archived answers. No inference or provider call is made.
    engine = Utf8Tokenizer()
    prefix = Prefix.__new__(Prefix)
    prefix.system = engine.encode("system")
    prefix.layers = [("source", engine.encode("a" * 25_900))]
    record = load("rate-limit", True)
    messages = []
    for turn in record["turns"][:8]:
        messages.extend([("user", f"Question {turn['turn']}"), ("assistant", turn["text"] * 8)])
    current = "Preserve this request in full, including its final clause."
    messages.append(("user", current))
    args = (engine, {"source": "publisher"}, record["tenant"], messages, True)
    original = prefix.build(*args)
    fitted = window_prompt(engine, prefix, *args[1:], 640)
    assert len(original.ids) + 640 > 31_000 >= len(fitted.ids) + 640
    assert original.spans[:-1] == fitted.spans[:-1]
    offset = fitted.spans[-1]["start"]
    assert original.ids[:offset] == fitted.ids[:offset]
    body = bytes(fitted.ids[offset:]).decode()
    assert "Earlier exchange summary (answer excerpts)" in body
    assert body.count("<|im_start|>user\n") == 7
    assert body.count("<|im_start|>assistant\n") == 7
    assert current in body
    assert "Question 1" not in body and all(f"Question {i}" in body for i in range(3, 9))


def test_oversized_current_request_stops_without_truncating_it():
    engine = Utf8Tokenizer()
    prefix = Prefix.__new__(Prefix)
    prefix.system = engine.encode("system")
    prefix.layers = []
    messages = [("user", "x" * 31_000)]
    with pytest.raises(ValueError, match="Shorten the task"):
        window_prompt(engine, prefix, {}, "1234abcd", messages, False, 640)
