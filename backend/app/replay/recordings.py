"""Replay only attested, shareable built-in coding sessions. Payments are always live."""
import hashlib
import json
from pathlib import Path

from backend.app.config import ROOT, sources
from backend.app.agents.tasks import STEPS, TASKS

DIRECTORY = ROOT / "backend/app/replay/traces"


def signature(task_id, buy):
    slug, text = TASKS[task_id]
    spec = [text, STEPS, buy, [(s, d["ids_sha256"]) for s,d in sources().items()]]
    return hashlib.sha256(json.dumps(spec,ensure_ascii=False).encode()).hexdigest()


def path(task_id, buy):
    if task_id not in TASKS:
        raise ValueError("Only recorded built-in tasks support replay")
    return DIRECTORY / f"{task_id}-{'buy' if buy else 'compute'}.json"


def load(task_id, buy):
    data = json.loads(path(task_id,buy).read_text())
    if (data["signature"] != signature(task_id,buy) or data["task_id"] != task_id
            or data["buy"] is not buy or len(data["turns"]) != len(STEPS)):
        raise ValueError("Recorded session differs from the current task and contexts")
    for t in data["turns"]:
        if (len(t["input_hash"]) != 64 or not t["text"] or not 0 <= t["cached_tokens"] <= t["prompt_tokens"]
                or t["output_tokens"] <= 0 or t["ttft_ms"] <= 0):
            raise ValueError("Incomplete GPU recording")
    return data


def available():
    try:
        for key in TASKS:
            for buy in (True,False):
                load(key,buy)
        return True
    except (FileNotFoundError,KeyError,ValueError):
        return False
