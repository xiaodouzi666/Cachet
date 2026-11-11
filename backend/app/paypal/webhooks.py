"""Provider event names and evidence recorded by the signature-verifying receiver."""

import time
from urllib.parse import urlsplit

import httpx

from .client import resource_id, settings

EVENTS = frozenset({
    "CHECKOUT.ORDER.APPROVED",
    "PAYMENT.AUTHORIZATION.CREATED",
    "PAYMENT.AUTHORIZATION.VOIDED",
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.REFUNDED",
    "PAYMENT.PAYOUTSBATCH.SUCCESS",
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
})


def expected_deliveries(wallet, payouts):
    """Map each operation in the two successful smoke journals to its PayPal event."""
    if not wallet.get("completed") or not payouts.get("completed"):
        raise ValueError("Both sandbox smoke journals must be complete.")
    w, p = wallet["readbacks"], payouts["readbacks"]
    def ids(journal, *operations):
        distinct = {resource_id(journal[op]["id"]) for op in operations}
        if len(distinct) != len(operations):
            raise ValueError("Multiple smoke operations reference the same PayPal resource: " + ", ".join(operations))
        return distinct

    standard_voids = ("partial.void", "unused.void")
    extra_voids = [key for key in w if key.endswith(".void") and key not in standard_voids]
    return {
        "CHECKOUT.ORDER.APPROVED": ids(w, "multi.order", "partial.order", "unused.order"),
        "PAYMENT.AUTHORIZATION.CREATED": ids(w, "multi.authorization", "partial.authorization", "unused.authorization"),
        "PAYMENT.AUTHORIZATION.VOIDED": ids(w, *standard_voids, *extra_voids),
        "PAYMENT.CAPTURE.COMPLETED": ids(w, "multi.first.capture", "multi.second.capture",
                                        "multi.final.capture", "partial.first.capture"),
        "PAYMENT.CAPTURE.REFUNDED": ids(w, "multi.refund"),
        "PAYMENT.PAYOUTSBATCH.SUCCESS": ids(p, "payout.batch"),
        "PAYMENT.PAYOUTS-ITEM.SUCCEEDED": ids(p, "payout.starlette", "payout.fastapi"),
    }


def verified_receipt(session, identifier, event_type=None):
    """Project a verified row, including PayPal's alternate payout resource keys."""
    from sqlalchemy import or_, select
    from backend.app.models import WebhookEvent

    rid = resource_id(identifier)
    matches = [WebhookEvent.resource_id == rid]
    query = select(WebhookEvent.paypal_event_id, WebhookEvent.event_type,
                   WebhookEvent.received_at, WebhookEvent.processed_at).where(WebhookEvent.verified.is_(True))
    if event_type is not None:
        if event_type not in EVENTS:
            raise ValueError("Unknown PayPal webhook event type.")
        query = query.where(WebhookEvent.event_type == event_type)
        resource = WebhookEvent.raw_json["resource"]
        matches.append(resource["id"].as_string() == rid)
        if event_type == "PAYMENT.PAYOUTSBATCH.SUCCESS":
            matches.extend([resource["payout_batch_id"].as_string() == rid,
                            resource["batch_header"]["payout_batch_id"].as_string() == rid])
        elif event_type == "PAYMENT.PAYOUTS-ITEM.SUCCEEDED":
            matches.append(resource["payout_item_id"].as_string() == rid)
    row = session.execute(query.where(or_(*matches)).order_by(
        WebhookEvent.received_at.desc(), WebhookEvent.id.desc()).limit(1)).first()
    if row is None:
        return None
    return {"event_id": row.paypal_event_id, "type": row.event_type, "resource_id": rid,
            "verified": True, "received_at": row.received_at.isoformat(),
            "processed_at": row.processed_at.isoformat() if row.processed_at else None}


def _collect_receipts(expected, read):
    evidence, missing = [], {}
    event_ids = set()
    for event_type, identifiers in expected.items():
        for rid in sorted(identifiers):
            receipt = read(resource_id(rid), event_type)
            if (not receipt or receipt.get("verified") is not True or receipt.get("type") != event_type
                    or receipt.get("resource_id") != rid or not receipt.get("event_id") or not receipt.get("received_at")):
                missing.setdefault(event_type, []).append(rid)
                continue
            if receipt["event_id"] in event_ids:
                raise ValueError("A single PayPal event cannot attest two journal operations.")
            event_ids.add(receipt["event_id"])
            evidence.append({"event_id": receipt["event_id"], "event_type": event_type, "resource_ids": [rid],
                             "verified": True, "received_at": receipt["received_at"]})
    return {"verified_deliveries": evidence, "missing": missing}


def _remote_receipts(expected, config):
    # Only the separately bound receiver origin can receive the production operator key.
    origin = (config.get("PRODUCTION_API_URL") or "").rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or not config.get("PRODUCTION_ADMIN_TOKEN")):
        raise ValueError("Configure the bound HTTPS PRODUCTION_API_URL and its PRODUCTION_ADMIN_TOKEN.")
    with httpx.Client(base_url=origin, headers={"X-Admin-Token": config["PRODUCTION_ADMIN_TOKEN"]},
                      timeout=15, follow_redirects=False) as client:
        def read(rid, kind):
            response = client.get("/api/operator/webhook-receipts/" + rid, params={"event_type": kind})
            if response.status_code == 404:
                return None
            return response.raise_for_status().json()
        return _collect_receipts(expected, read)


def delivery_evidence(wallet, payouts):
    """Check the actual receiver bound to the sandbox subscription for these operations."""
    from backend.app.db import session_scope

    expected = expected_deliveries(wallet, payouts)
    config = settings()
    if config.get("PRODUCTION_PAYPAL_WEBHOOK_ID") and config["PRODUCTION_PAYPAL_WEBHOOK_ID"] == config.get("PAYPAL_WEBHOOK_ID"):
        return _remote_receipts(expected, config)
    with session_scope(write=False) as session:
        return _collect_receipts(expected, lambda rid, kind: verified_receipt(session, rid, kind))


def wait_for_deliveries(wallet, payouts, seconds=0):
    """Poll only the receiving evidence; a transport failure never repeats payment operations."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            evidence = delivery_evidence(wallet, payouts)
            if not evidence["missing"] or time.monotonic() >= deadline:
                return evidence
        except httpx.TransportError:
            if time.monotonic() >= deadline:
                raise
        except httpx.HTTPStatusError as exc:
            if (exc.response.status_code != 429 and exc.response.status_code < 500) or time.monotonic() >= deadline:
                raise
        time.sleep(min(3, max(0, deadline - time.monotonic())))


def replay_evidence(evidence, wait_seconds):
    """Request fresh signed sandbox redeliveries of late then early events for a closed acceptance run."""
    import time
    import uuid
    from sqlalchemy import func, select
    from backend.app.db import session_scope
    from backend.app.models import LedgerTxn, Mandate, WebhookEvent
    from backend.app.operations import Control
    from backend.app.paypal.client import PayPalClient, request_id
    from backend.app.paypal.reconciliation import reconcile

    target = next((s for s in reversed(evidence.get("sessions", [])) if s.get("accepted")), None)
    if not target:
        raise ValueError("Provide a completed real acceptance evidence report.")
    ws = evidence["workspace_id"]
    refs = [("PAYMENT.CAPTURE.REFUNDED", target["refunds"][0]["paypal_refund_id"]),
            ("PAYMENT.CAPTURE.COMPLETED", target["captures"][0]["paypal_capture_id"]),
            ("CHECKOUT.ORDER.APPROVED", target["order_id"])]

    def snapshot():
        with session_scope(write=False) as db:
            m = db.get(Mandate, target["mandate_id"])
            if not m or m.workspace_id != ws or m.status != "CLOSED":
                raise ValueError("Evidence does not refer to a closed mandate in this database.")
            txns = db.scalar(select(func.count()).select_from(LedgerTxn).where(LedgerTxn.workspace_id == ws))
            return (txns, m.status, m.captured_micros, m.refunded_micros, m.closed_at)

    before = snapshot()
    replayed = []
    with PayPalClient.from_env() as pp:
        for kind, rid in refs:
            with session_scope(write=False) as db:
                row = db.scalar(select(WebhookEvent).where(WebhookEvent.verified.is_(True), WebhookEvent.event_type == kind,
                                WebhookEvent.resource_id == rid, WebhookEvent.processed_at.is_not(None)))
                if not row:
                    raise ValueError(f"No processed, verified original for {kind}.")
                key = "webhook-deliveries:" + row.paypal_event_id
                counter = db.get(Control, key)
                count = counter.data["count"] if counter else 0
            pp.resend_webhook(row.paypal_event_id, request_id("resend", row.paypal_event_id, str(uuid.uuid4())))
            deadline = time.monotonic() + wait_seconds
            while True:
                with session_scope(write=False) as db:
                    counter = db.get(Control, key)
                    if counter and counter.data["count"] > count:
                        if db.get(WebhookEvent, row.id).processed_at != row.processed_at:
                            raise RuntimeError("A repeated event changed its completed projection.")
                        replayed.append({"event_id":row.paypal_event_id, "event_type":kind, "verified":True})
                        break
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"No confirmed PayPal redelivery for {kind} before timeout.")
                time.sleep(2)
    if snapshot() != before or reconcile(ws)["ok"] is not True:
        raise RuntimeError("Late event redelivery changed settled records or reconciliation.")
    return {"late_then_early_verified_redeliveries":replayed, "workspace_ledger_unchanged":True}
