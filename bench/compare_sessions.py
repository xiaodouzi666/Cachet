"""Price actual GPU recordings at the published default rates; no payment is initiated."""

import json
from statistics import median

from backend.app.agents.tasks import TASKS
from backend.app.config import ROOT, band_for, calibration
from backend.app.gateway.billing import Span, price_call, sla_breached
from backend.app.replay.recordings import load, path


def compare(task, buy):
    track = load(task, buy)
    turns = track["turns"]
    profile = calibration()
    fit = profile["fit"]
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
    breach_turns = []
    compliant_royalties = {}
    for turn, invoice in zip(turns, invoices):
        # The builder and pricer enforce a contiguous public prefix before private spans.
        public_end = max((s["end"] for s in turn["spans"] if s["kind"] != "private"), default=0)
        breached = sla_breached(
            min(public_end, turn["cached_tokens"]), turn["ttft_ms"],
            band_for(turn["prompt_tokens"], profile)["warm_p50_ms"], factor=2,
        )
        if breached:
            breach_turns.append(turn["turn"])
        for owner, amount in invoice.royalty_micros.items():
            compliant_royalties.setdefault(owner, 0)
            if not breached:
                compliant_royalties[owner] += amount

    return dict(
        task=task,
        decision="buy" if buy else "compute",
        turns=len(turns),
        generated_application_check=track["application_check"],
        verified_application_turn=track["verified_application_turn"],
        source=path(task, buy).relative_to(ROOT).as_posix(),
        first_ttft_ms=turns[0]["ttft_ms"],
        median_ttft_ms=round(median(t["ttft_ms"] for t in turns), 3),
        cached_tokens=sum(t["cached_tokens"] for t in turns),
        output_tokens=sum(t["output_tokens"] for t in turns),
        priced_usage_micros=sum(i.charge_micros for i in invoices),
        sla_breach_turns=breach_turns,
        royalties_on_sla_compliant_turns_micros=compliant_royalties,
        publisher_royalty_micros=sum(sum(i.royalty_micros.values()) for i in invoices),
    )


if __name__ == "__main__":
    rows = [compare(task, buy) for task in TASKS for buy in [True, False]]
    (ROOT / "bench/results/sessions.json").write_text(
        json.dumps(
            {
                "method": "Four separate GPU conversations evaluated at the default tariff. SLA breaches use twice the nearest calibrated warm p50 and require a public hit. Royalties on compliant turns are metered estimates before payment rounding. No funds moved.",
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
