"""Workspace-scoped signals computed from actual meter readings."""

import hashlib
import statistics
from datetime import timedelta

from sqlalchemy import select

from ..models import Call, Consumer, Publisher, utcnow
from ..operations import Control


def payer_key(mandate):
    return f"mandate-payer:{mandate.workspace_id}:{mandate.id}"


def receiver_digest(address):
    if not isinstance(address, str) or not address.strip():
        return None
    return hashlib.sha256(address.strip().casefold().encode("utf-8")).hexdigest()


def remember_authorized_payer(db, mandate, order):
    """Bind identity to the exact PayPal order just verified by the authorization service."""
    if not mandate.paypal_order_id or not mandate.authorization_id or order.get("id") != mandate.paypal_order_id:
        raise ValueError("Cannot bind a payer without the matching PayPal order and authorization")
    key = payer_key(mandate)
    if db.get(Control, key):
        return
    payer = order.get("payer") or order.get("payment_source", {}).get("paypal", {})
    payer_id = payer.get("payer_id") or payer.get("account_id")
    # A card authorization may have no wallet identity. Keep this fact distinct
    # from a legacy mandate whose identity has never been looked up.
    fingerprint = receiver_digest(payer.get("email_address")) if payer_id else None
    db.add(Control(key=key, data={"payer_id": payer_id, "email_digest": fingerprint}))
    if payer_id:
        db.get(Consumer, mandate.consumer_id).linked_payer_id = payer_id
        if fingerprint:
            for publisher in db.scalars(select(Publisher).where(Publisher.workspace_id == mandate.workspace_id)):
                if receiver_digest(publisher.paypal_receiver_email) == fingerprint:
                    publisher.linked_payer_id = payer_id


def same_payer_layers(identity, publishers):
    """Determine self-use from this mandate's verified account, including later publishers."""
    payer_id = identity.get("payer_id")
    if not payer_id:
        return set()
    fingerprint = identity.get("email_digest")
    return {slug for slug, p in publishers.items()
            if payer_id == p.linked_payer_id or
            (fingerprint and receiver_digest(p.paypal_receiver_email) == fingerprint)}


def volume_signal(observed, history):
    if len(history) < 7:
        return None
    deviation = statistics.pstdev(history)
    if not deviation:
        return None
    return (observed - statistics.mean(history)) / deviation


def activity(db, workspace_id, now=None):
    now = now or utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now - today
    start = today - timedelta(days=7)
    publishers = list(db.scalars(select(Publisher).where(Publisher.workspace_id == workspace_id)))
    counts = {p.id: [0] * 8 for p in publishers}
    self_hits = {p.id: 0 for p in publishers}
    calls = db.scalars(select(Call).where(Call.workspace_id == workspace_id, Call.created_at.between(start, now)))
    for call in calls:
        day = (call.created_at - start).days
        if call.created_at - (start + timedelta(days=day)) > elapsed:
            continue
        for span in call.spans_json:
            if span["kind"] == "publisher" and span["owner"] in counts:
                hits = max(0, min(span["end"], call.cached_tokens) - span["start"])
                counts[span["owner"]][day] += hits
                if day == 7 and span.get("self_royalty_suppressed"):
                    self_hits[span["owner"]] += hits
    return [
        {
            "publisher_id": p.id, "name": p.name, "today_hits": counts[p.id][-1],
            "reference_hits": counts[p.id][:-1], "self_use_hits": self_hits[p.id],
            "z_score": volume_signal(counts[p.id][-1], counts[p.id][:-1]) if p.created_at <= start else None,
        }
        for p in publishers
    ]
