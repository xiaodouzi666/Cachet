"""Capture shareable built-in GPU sessions, or reconstruct all captured input hashes."""

import argparse
import fcntl
import json
import os
import secrets
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

from scripts.check_generated_apps import assess_record, proposal_feedback
from backend.app.agents.inference import live_turns
from backend.app.agents.quote_reason import canonical_quote
from backend.app.agents.tasks import MAX_OUTPUT_TOKENS, STEPS, TASKS
from backend.app.config import manifest
from backend.app.gateway.prompt_builder import Prefix
from backend.app.gateway.quote import quote
from backend.app.gateway.sglang_client import Engine
from backend.app.replay.recordings import (
    DIRECTORY,
    SCHEMA,
    digest,
    load,
    path,
    signature,
    validate,
    verify_inputs,
)

OUTPUT_LIMIT = MAX_OUTPUT_TOKENS


def capture(engine, task_id, buy, model, *, public_salt=None):
    slug, task = TASKS[task_id]
    prefix = Prefix(engine, slug)
    owners = {name: name for name, _ in prefix.layers}
    engine.flush()
    if buy and prefix.warm(engine) < 0.95:
        raise ValueError("Warm probe missed its threshold")
    # Only public archive salts enter these recordings.
    tenant = public_salt if public_salt is not None else secrets.token_hex(4)
    q = canonical_quote(quote(sum(len(ids) for _, ids in prefix.layers), len(STEPS), 0.25 if buy else 0))
    results = live_turns(
        engine,
        prefix,
        owners,
        tenant,
        task,
        buy,
        max_output=OUTPUT_LIMIT,
        check_proposal=lambda text: proposal_feedback(task_id, text),
    )
    record = {
        "schema": SCHEMA,
        "task_id": task_id,
        "source": slug,
        "buy": buy,
        "quote": q,
        "model": model,
        "tenant": tenant,
        "output_limit": OUTPUT_LIMIT,
        "signature": signature(task_id, buy),
        "turns": [t for t in results if t["kind"] == "result"],
    }
    record["recorded_at"] = datetime.now(timezone.utc).isoformat()
    record.update(assess_record(record))
    validate(record, task_id, buy)
    verify_inputs(engine, record)
    return record


def publish(updates):
    """Readers fail closed if they observe an update between file and digest replacements."""
    index = DIRECTORY / "sha256.json"
    with TemporaryDirectory(dir=DIRECTORY) as working:
        temp = Path(working)
        hashes = json.loads(index.read_bytes()) if index.exists() else {}
        for target, record in updates:
            raw = (json.dumps(record, indent=2) + "\n").encode()
            (temp / target.name).write_bytes(raw)
            hashes[target.name] = digest(raw)
        (temp / index.name).write_text(
            json.dumps(hashes, sort_keys=True, indent=2) + "\n"
        )
        for target, _ in updates:
            (temp / target.name).replace(target)
        (temp / index.name).replace(index)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=TASKS)
    parser.add_argument(
        "--decision", choices=("buy", "compute"),
        help="Select only this strategy. By default, process both.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Re-tokenize the checked-in inputs without generating new completions.",
    )
    parser.add_argument(
        "--reuse-recorded-salt", action="store_true",
        help="Regenerate with the public salt from the selected archived run for a controlled comparison.",
    )
    args = parser.parse_args()
    if args.reuse_recorded_salt and (args.verify_only or not args.task or not args.decision):
        parser.error("--reuse-recorded-salt requires --task and --decision in capture mode")
    tasks = [args.task] if args.task else list(TASKS)
    decisions = (True, False) if args.decision is None else (args.decision == "buy",)
    # A directory lock avoids concurrent recorders flushing the same cache or losing digest updates.
    directory_fd = os.open(DIRECTORY, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with Engine() as engine:
            model = (
                engine.get("/model_info").raise_for_status().json()["served_model_name"]
            )
            if model != manifest()["tokenizer"]["model_id"]:
                raise ValueError("SGLang is serving a different model")
            updates = []
            for task_id in tasks:
                for buy in decisions:
                    output = path(task_id, buy)
                    if args.verify_only:
                        verify_inputs(engine, load(task_id, buy))
                    else:
                        salt = load(task_id, buy)["tenant"] if args.reuse_recorded_salt else None
                        record = capture(engine, task_id, buy, model, public_salt=salt)
                        updates.append((output, record))
                    print(output.name, len(STEPS), "input hashes verified", flush=True)
            if updates:
                publish(updates)
                for task_id in tasks:
                    for buy in decisions:
                        load(task_id, buy)
    finally:
        os.close(directory_fd)


if __name__ == "__main__":
    main()
