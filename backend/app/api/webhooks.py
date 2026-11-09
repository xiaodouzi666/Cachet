"""Verify PayPal deliveries, then apply confirmed resource state to existing local intents."""

import json
import logging
import threading
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import or_, select
from starlette.concurrency import run_in_threadpool

from backend.app import ledger
from backend.app.db import session_scope
from backend.app.models import Capture, Mandate, PayoutBatch, Refund, WebhookEvent, utcnow
from backend.app.operations import Control, Operation
from backend.app.paypal.client import PayPalClient, resource_id
from backend.app.paypal.webhooks import EVENTS
from backend.app.settlement.mandate_fsm import transition
from backend.app.settlement.payout_job import refresh_batch
from backend.app.settlement.service import authorize, cents, lock
from backend.app.money import round_cents

router = APIRouter()
_log = logging.getLogger(__name__)
_event_locks = [threading.Lock() for _ in range(71)]
MAX_BODY = 262144


def identity(resource):
    return (resource.get("id") or resource.get("payout_item_id") or resource.get("payout_batch_id")
            or (resource.get("batch_header") or {}).get("payout_batch_id"))


def find_mandate(db, event):
    resource = event["resource"]
    rid = identity(resource)
    related = (resource.get("supplementary_data") or {}).get("related_ids") or {}
    if event["event_type"] == "CHECKOUT.ORDER.APPROVED":
        return db.scalar(select(Mandate).where(Mandate.paypal_order_id == rid))
    match = Mandate.authorization_id == rid
    if related.get("order_id"):
        match = or_(match, Mandate.paypal_order_id == related["order_id"])
    return db.scalar(select(Mandate).where(match))


def mandate_event(pp, event):
    with session_scope(write=False) as db:
        mandate = find_mandate(db, event)
    if not mandate:
        return False
    with lock(mandate.workspace_id):
        kind = event["event_type"]
        if kind == "CHECKOUT.ORDER.APPROVED":
            order = pp.get_order(mandate.paypal_order_id)
            if order["status"] not in {"APPROVED", "COMPLETED"}:
                return False
            with session_scope() as db:
                m = db.get(Mandate, mandate.id)
                if m.status == "CREATED":
                    transition(m, "APPROVED")
            return True
        aid = identity(event["resource"])
        remote = pp.get_authorization(aid)
        if cents(remote["amount"]) * 10000 != mandate.cap_micros:
            raise ValueError("PayPal authorization differs from the mandate.")
        if kind == "PAYMENT.AUTHORIZATION.CREATED":
            if mandate.authorization_id:
                return mandate.authorization_id == aid
            # A verified event may arrive before the explicit authorize call persists its result.
            with session_scope(write=False) as db:
                op = db.scalar(select(Operation).where(Operation.kind == "authorize", Operation.subject == mandate.id))
            if not op or remote["status"] != "CREATED":
                return False
            order = pp.get_order(mandate.paypal_order_id)
            linked = {a["id"] for u in order.get("purchase_units", []) for a in u.get("payments", {}).get("authorizations", [])}
            if linked != {aid}:
                return False
            authorize(mandate.workspace_id, mandate.id)
            return True
        if remote["status"] != "VOIDED" or mandate.authorization_id != aid:
            return False
        with session_scope() as db:
            m = db.get(Mandate, mandate.id)
            if m.status in {"VOIDED", "CLOSED", "EXPIRED"}:
                return True
            # An externally voided authorization with an open bill needs operator reconciliation.
            if m.status not in {"ACTIVE", "AUTHORIZED", "CLOSING"} or round_cents(m.accrued_uncaptured_micros):
                return False
            if m.status != "CLOSING":
                transition(m, "CLOSING")
            ledger.close_dust(db, m)
            transition(m, "CLOSED" if m.captured_micros else "VOIDED")
            return True


def money_event(pp, event):
    resource = event["resource"]
    rid = identity(resource)
    capture = event["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
    model, column, kind = ((Capture, Capture.paypal_capture_id, "capture") if capture
                           else (Refund, Refund.paypal_refund_id, "refund"))
    correlation = resource.get("invoice_id" if capture else "custom_id")
    with session_scope(write=False) as db:
        row = db.scalar(select(model).where(column == rid))
        op = db.scalar(select(Operation).where(Operation.kind == kind, or_(Operation.external_id == rid, Operation.id == correlation)))
    if not row and not op:
        return False
    with lock(row.workspace_id if row else op.workspace_id):
        remote = pp.get_capture(rid) if capture else pp.get_refund(rid)
        allowed = {"COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED"} if capture else {"COMPLETED"}
        if remote.get("id") != rid or remote.get("status") not in allowed:
            return False
        amount = cents(remote["amount"])
        with session_scope() as db:
            row = db.scalar(select(model).where(column == rid))
            if row:
                if row.amount_cents != amount:
                    raise ValueError("PayPal event amount differs from the posted entry.")
                if capture:
                    row.status = remote["status"]
                return True
            op = db.get(Operation, op.id)
            if op.external_id != rid and remote.get("invoice_id" if capture else "custom_id") != op.id:
                return False
            if op.external_id not in (None, rid) or op.payload["amount"] != amount:
                raise ValueError("PayPal resource differs from its prepared operation.")
            op.external_id = rid
            if capture:
                m = db.get(Mandate, op.subject)
                if m.status not in {"CAPTURING", "CLOSING"}:
                    return False
                final = remote.get("final_capture")
                if final is not op.payload["final"]:
                    raise ValueError("PayPal capture final flag differs from its intent.")
                row = Capture(workspace_id=m.workspace_id, mandate_id=m.id, paypal_capture_id=rid,
                              status=remote["status"], amount_cents=amount, final=final,
                              fee_cents=cents(remote["seller_receivable_breakdown"]["paypal_fee"]))
                ledger.record_capture(db, row)
                if m.status == "CAPTURING":
                    transition(m, "ACTIVE")
                op.result = {"id":rid}
            else:
                cap = db.get(Capture, op.subject)
                fee = (remote.get("seller_payable_breakdown") or {}).get("paypal_fee")
                row = Refund(workspace_id=op.workspace_id, capture_id=cap.id, paypal_refund_id=rid, amount_cents=amount,
                             status="COMPLETED", fee_reversal_cents=abs(cents(fee)) if fee else 0,
                             reason=op.payload["reason"], call_ids_json=op.payload["calls"])
                ledger.record_refund(db, row)
                op.result = {"id":row.id, "paypal_refund_id":rid, "amount_cents":amount}
            return True


def project(pp, event):
    kind = event["event_type"]
    if kind not in EVENTS:
        return True
    if kind in ("PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.REFUNDED"):
        return money_event(pp, event)
    if kind.startswith("PAYMENT.PAYOUT"):
        resource = event["resource"]
        header = resource.get("batch_header") or {}
        batch_id = header.get("payout_batch_id") or resource.get("payout_batch_id")
        if not batch_id:
            return False
        with session_scope(write=False) as db:
            b = db.scalar(select(PayoutBatch).where(PayoutBatch.paypal_batch_id == batch_id))
        if not b:
            return False
        with lock(b.workspace_id):
            refresh_batch(pp, b.workspace_id, b.id)
        return True
    return mandate_event(pp, event)


def process(pp, event_id):
    with _event_locks[hash(event_id) % len(_event_locks)]:
        with session_scope(write=False) as db:
            row = db.scalar(select(WebhookEvent).where(WebhookEvent.paypal_event_id == event_id, WebhookEvent.verified.is_(True)))
        if row.processed_at:
            return True
        # The stored event is immutable. Projectors consult current PayPal GET state and existing local intents.
        if not project(pp, row.raw_json):
            return False
        with session_scope() as db:
            db.get(WebhookEvent, row.id).processed_at = utcnow()
        return True


def retry_pending():
    with session_scope(write=False) as db:
        ids = list(db.scalars(select(WebhookEvent.paypal_event_id).where(WebhookEvent.processed_at.is_(None),
                    WebhookEvent.verified.is_(True), WebhookEvent.received_at > utcnow() - timedelta(days=7))))
    if ids:
        with PayPalClient.from_env() as pp:
            for event_id in ids:
                try:
                    process(pp, event_id)
                except Exception:
                    _log.exception("Could not project verified PayPal event %s", event_id)


def receive(headers, event):
    try:
        if not isinstance(event, dict) or not isinstance(event.get("resource"), dict) or not isinstance(event.get("event_type"), str):
            raise ValueError
        event_id = resource_id(event.get("id"))
    except ValueError:
        raise HTTPException(400, "Malformed PayPal webhook")
    signed = {k.lower():v for k, v in headers.items()}
    if any(not signed.get(k) for k in ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                                       "paypal-transmission-sig", "paypal-transmission-time")):
        raise HTTPException(401, "Missing PayPal webhook signature")
    with PayPalClient.from_env() as pp:
        if not pp.verify_webhook(signed, event):
            raise HTTPException(401, "Invalid PayPal webhook signature")
        with session_scope() as db:
            row = db.scalar(select(WebhookEvent).where(WebhookEvent.paypal_event_id == event_id))
            duplicate = row is not None
            if row:
                if row.event_type != event["event_type"] or row.resource_id != (identity(event["resource"]) or ""):
                    raise HTTPException(409, "Conflicting PayPal event identity")
            else:
                db.add(WebhookEvent(paypal_event_id=event_id, event_type=event["event_type"],
                                    resource_id=identity(event["resource"]) or "", verified=True, raw_json=event))
            key = "webhook-deliveries:" + event_id
            marker = db.get(Control, key)
            count = marker.data["count"] if marker else 0
            db.merge(Control(key=key, data={"count":count + 1}))
        applied = process(pp, event_id)
        return {"received":True, "duplicate":duplicate, "projected":applied}


@router.post("/api/webhooks/paypal")
async def webhook(request: Request):
    parts = bytearray()
    async for part in request.stream():
        if len(parts) + len(part) > MAX_BODY:
            raise HTTPException(413, "Webhook too large")
        parts.extend(part)
    try:
        event = json.loads(parts)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON")
    return await run_in_threadpool(receive, dict(request.headers), event)
