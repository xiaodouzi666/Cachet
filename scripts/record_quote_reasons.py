"""Record the four model-generated quote explanations or verify their original input IDs."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.app.agents.quote_reason import (
    ARCHIVE,
    generate_reason,
    prompt_for,
    quote_key,
    recorded_reason,
)
from backend.app.agents.tasks import TASKS
from backend.app.gateway.sglang_client import Engine
from backend.app.replay.recordings import load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Retokenize each original explanation prompt.",
    )
    args = parser.parse_args()
    receipts = {}
    with Engine() as engine:
        for task in TASKS:
            for buy in (True, False):
                quote = load(task, buy)["quote"]
                receipt = (
                    recorded_reason(quote)
                    if args.verify_only
                    else generate_reason(engine, quote)
                )
                if (
                    engine.encode(prompt_for(quote, receipt["retry"]))
                    != receipt["input_ids"]
                ):
                    raise ValueError(
                        "Re-tokenized quote prompt differs from its original input IDs"
                    )
                receipts[quote_key(quote)] = receipt
                print(
                    task,
                    quote["decision"],
                    receipt["input_hash"],
                    receipt["reason"],
                    flush=True,
                )
    if not args.verify_only:
        with TemporaryDirectory(dir=ARCHIVE.parent) as working:
            temp = Path(working) / ARCHIVE.name
            temp.write_text(json.dumps(receipts, indent=2, sort_keys=True) + "\n")
            temp.chmod(0o644)
            temp.replace(ARCHIVE)


if __name__ == "__main__":
    main()
