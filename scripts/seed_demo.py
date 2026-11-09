"""Accumulate genuine operator test sessions and outstanding royalties in one judge workspace."""

import argparse
import httpx

from e2e_demo import arguments, execute, now
from sandbox_smoke import check


def seed(demo):
    for _ in range(120):
        rows = [s for s in demo.sessions if s["purpose"] == "seed" and s.get("finished")]
        run_ids = {s["run_id"] for s in rows}
        check(len(run_ids) == len(rows), "Duplicate seed run IDs in the private journal.")
        payable = demo.payable()
        if len(run_ids) >= 30 and min(payable.values()) >= 50_000:
            demo.data["seed"] = {"at":now(), "distinct_live_sessions":len(run_ids),
                                 "outstanding_micros":payable, "reconciliation":demo.reconcile()}
            demo.journal.save()
            return
        demo.complete_session("seed")
    raise RuntimeError("Seed limit reached without 30 distinct live sessions and at least $0.05 payable per publisher.")


if __name__ == "__main__":
    parser = arguments(argparse.ArgumentParser(description=__doc__))
    try:
        execute(parser.parse_args(), seed)
    except (RuntimeError, ValueError, httpx.HTTPError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
