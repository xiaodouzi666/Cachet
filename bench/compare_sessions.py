"""Price actual GPU recordings at the published default rates; no payment is initiated."""

import json
from statistics import median

from backend.app.agents.tasks import TASKS
from backend.app.config import ROOT, calibration
from backend.app.gateway.billing import Span, price_call
from backend.app.replay.recordings import load, path


def compare(task, buy):
    track = load(task, buy)
    turns = track["turns"]
    fit = calibration()["fit"]
    invoices = [
        price_call(
            spans=[Span(**span) for span in turn["spans"]],
            cached_tokens=turn["cached_tokens"],
            output_tokens=turn["output_tokens"],
            alpha=fit["alpha"],
            beta=fit["beta"],
        )
        for turn in turns
    ]
    return dict(
        task=task,
        decision="buy" if buy else "compute",
        turns=len(turns),
        source=path(task, buy).relative_to(ROOT).as_posix(),
        first_ttft_ms=turns[0]["ttft_ms"],
        median_ttft_ms=round(median(t["ttft_ms"] for t in turns), 3),
        cached_tokens=sum(t["cached_tokens"] for t in turns),
        output_tokens=sum(t["output_tokens"] for t in turns),
        priced_usage_micros=sum(i.charge_micros for i in invoices),
        publisher_royalty_micros=sum(sum(i.royalty_micros.values()) for i in invoices),
    )


if __name__ == "__main__":
    rows = [compare(task, buy) for task in TASKS for buy in [True, False]]
    (ROOT / "bench/results/sessions.json").write_text(
        json.dumps(
            {
                "method": "Four separate deterministic GPU conversations. Default tariff applied to the recorded counters. No funds moved.",
                "runs": rows,
            },
            indent=2,
        )
        + "\n"
    )
    for row in rows:
        print(
            row["task"],
            row["decision"],
            row["first_ttft_ms"],
            row["priced_usage_micros"],
        )
