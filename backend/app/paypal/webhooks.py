"""Provider event names used by the local receiver and its registration."""

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
    return {
        "CHECKOUT.ORDER.APPROVED": {w[key + ".order"]["id"] for key in ("multi", "partial", "unused")},
        "PAYMENT.AUTHORIZATION.CREATED": {w[key + ".authorization"]["id"] for key in ("multi", "partial", "unused")},
        "PAYMENT.AUTHORIZATION.VOIDED": {v["id"] for key, v in w.items() if key.endswith(".void")},
        "PAYMENT.CAPTURE.COMPLETED": {w[key + ".capture"]["id"] for key in
                                      ("multi.first", "multi.second", "multi.final", "partial.first")},
        "PAYMENT.CAPTURE.REFUNDED": {w["multi.refund"]["id"]},
        "PAYMENT.PAYOUTSBATCH.SUCCESS": {p["payout.batch"]["id"]},
        "PAYMENT.PAYOUTS-ITEM.SUCCEEDED": {p[key]["id"] for key in ("payout.starlette", "payout.fastapi")},
    }


def delivery_evidence(wallet, payouts):
    """Read only verified deliveries observed by our actual receiver, omitting personal data."""
    from sqlalchemy import select
    from backend.app.db import session_scope
    from backend.app.models import WebhookEvent

    expected = expected_deliveries(wallet, payouts)
    found = {kind: set() for kind in expected}
    evidence = []
    with session_scope(write=False) as session:
        events = session.scalars(select(WebhookEvent).where(
            WebhookEvent.verified.is_(True), WebhookEvent.event_type.in_(EVENTS),
        ))
        for event in events:
            payload = event.raw_json.get("resource", {})
            ids = {event.resource_id, payload.get("id"), payload.get("payout_item_id"),
                   payload.get("batch_header", {}).get("payout_batch_id")}
            matched = ids & expected[event.event_type]
            if matched:
                found[event.event_type].update(matched)
                evidence.append({"event_id": event.paypal_event_id, "event_type": event.event_type,
                                 "resource_ids": sorted(matched), "verified": True,
                                 "received_at": event.received_at.isoformat()})
    missing = {kind: sorted(expected[kind] - found[kind]) for kind in expected if expected[kind] - found[kind]}
    return {"verified_deliveries": evidence, "missing": missing}


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
