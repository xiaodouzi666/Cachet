"""Replay verified real events in reverse on an isolated database copy, then detect a local capture edit."""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.api.webhooks import process
from backend.app.db import configured_url, session_scope
from backend.app.models import Capture, LedgerTxn, WebhookEvent
from backend.app.paypal.client import PayPalClient
from backend.app.paypal.reconciliation import reconcile


def check(evidence):
    source = Path(make_url(configured_url()).database).resolve(strict=True)
    target = next((s for s in reversed(evidence["sessions"]) if s.get("accepted")), None)
    if not target:
        raise ValueError("A completed real acceptance run is required.")
    ws = evidence["workspace_id"]
    refs = {target["order_id"], target["authorization_id"], target["payout"]["paypal_batch_id"],
            *(r["paypal_capture_id"] for r in target["captures"]), *(r["paypal_refund_id"] for r in target["refunds"]),
            *(i["paypal_item_id"] for i in target["payout"]["items"])}
    with tempfile.TemporaryDirectory(prefix="cachet-reconcile-") as path:
        copy = Path(path) / "snapshot.db"
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        replica = sqlite3.connect(copy)
        try:
            original.backup(replica)
        finally:
            original.close()
            replica.close()
        os.environ["DATABASE_URL"] = "sqlite:///" + str(copy)
        baseline = reconcile(ws)
        if baseline["ok"] is not True:
            raise ValueError("The original workspace must first have a nonempty matched PayPal reconciliation.")
        with session_scope() as db:
            events = list(db.scalars(select(WebhookEvent).where(WebhookEvent.verified.is_(True),
                        WebhookEvent.resource_id.in_(refs), WebhookEvent.processed_at.is_not(None))))
            kinds = {r.event_type for r in events}
            if not {"CHECKOUT.ORDER.APPROVED", "PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.CAPTURE.COMPLETED",
                    "PAYMENT.CAPTURE.REFUNDED", "PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"} <= kinds:
                raise ValueError("This receiver lacks the verified events for the completed acceptance run.")
            events.sort(key=lambda e:e.raw_json["create_time"], reverse=True)
            for row in events:
                row.processed_at = None
            original_txns = set(db.scalars(select(LedgerTxn.id).where(LedgerTxn.workspace_id == ws)))
        with PayPalClient.from_env() as pp:
            for e in events:
                if not process(pp, e.paypal_event_id) or not process(pp, e.paypal_event_id):
                    raise RuntimeError("A reverse-order verified event did not project.")
        after = reconcile(ws)
        with session_scope() as db:
            txns = set(db.scalars(select(LedgerTxn.id).where(LedgerTxn.workspace_id == ws)))
            if after != baseline or txns != original_txns:
                raise RuntimeError("Reverse-order projection changed a reconciled workspace.")
            capture = db.get(Capture, target["captures"][0]["id"])
            if not capture or capture.workspace_id != ws or capture.paypal_capture_id not in refs:
                raise ValueError("The acceptance capture is missing from the snapshot.")
            capture.final = not capture.final
        drift = reconcile(ws)
        if drift["ok"] is not False or not any(d.get("id") == capture.id and d.get("field") == "final capture" for d in drift["differences"]):
            raise RuntimeError("Deliberate snapshot edit was not reported.")
        return {"workspace_id":ws, "provider_resources_checked":baseline["checked"],
                "reverse_order_verified_events":len(events), "ledger_unchanged_after_replay":True,
                "detected_snapshot_edit":{"capture_id":capture.id, "field":"final capture"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(check(json.loads(args.evidence.read_text())), indent=2))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
