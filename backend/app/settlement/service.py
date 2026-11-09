"""Provider-confirmed money movement with durable identities for retries."""

from datetime import timedelta
from decimal import Decimal
from threading import RLock
from fastapi import HTTPException
from sqlalchemy import select

from backend.app import ledger
from backend.app.agents.anomaly import payer_key, remember_authorized_payer
from backend.app.api.access import owned
from backend.app.config import value
from backend.app.db import session_scope
from backend.app.models import (
    AgentRun,
    Capture,
    Consumer,
    Mandate,
    Refund,
    Workspace,
    utcnow,
)
from backend.app.operations import Control, Operation
from backend.app.money import usd_to_micros
from backend.app.paypal.client import PayPalClient, capture_flag_matches, request_id
from backend.app.settlement.capture_policy import plan_capture
from backend.app.settlement.mandate_fsm import transition

_locks = [RLock() for _ in range(67)]


def lock(ws):
    return _locks[hash(ws) % len(_locks)]


def cents(amount):
    if (
        not isinstance(amount, dict)
        or (amount.get("currency_code") or amount.get("currency")) != "USD"
    ):
        raise ValueError("Expected a PayPal USD amount")
    n = Decimal(str(amount["value"])) * 100
    if not n.is_finite() or n != n.to_integral_value():
        raise ValueError("PayPal amount is not in whole cents")
    return int(n)


def prepare(db, ws, kind, subject, payload, key):
    identifier = request_id(ws, kind, subject, str(key))
    op = db.get(Operation, identifier)
    if op is None:
        op = Operation(
            id=identifier, workspace_id=ws, kind=kind, subject=subject, payload=payload
        )
        db.add(op)
        db.flush()
    elif op.payload != payload:
        raise HTTPException(409, "Operation retry differs from its persisted request")
    return op


def stamp(op_id, external_id):
    if not external_id:
        raise ValueError("PayPal response omitted its resource ID")
    with session_scope() as db:
        op = db.get(Operation, op_id)
        if op.external_id and op.external_id != external_id:
            raise ValueError("Conflicting PayPal history")
        op.external_id = external_id


def call_once(op, send):
    if op.external_id:
        return op.external_id
    created = op.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=utcnow().tzinfo)
    if utcnow() - created > timedelta(hours=5):
        raise HTTPException(
            409, "Unconfirmed provider operation requires reconciliation before retry"
        )
    external = send()["id"]
    stamp(op.id, external)
    return external


def view(m):
    return {
        k: getattr(m, k)
        for k in (
            "id",
            "paypal_order_id",
            "authorization_id",
            "cap_micros",
            "captured_micros",
            "accrued_uncaptured_micros",
            "refunded_micros",
            "status",
            "payment_method",
            "closed_at",
        )
    } | {
        "mandate_id": m.id,
        "released_micros": m.cap_micros - m.captured_micros
        if m.authorization_id and m.status in {"CLOSED", "VOIDED", "EXPIRED"}
        else 0,
    }


def open_order(ws, cap, card=False):
    with lock(ws), PayPalClient.from_env() as pp:
        with session_scope() as db:
            consumer = db.scalar(select(Consumer).where(Consumer.workspace_id == ws))
            m = db.scalar(
                select(Mandate).where(
                    Mandate.consumer_id == consumer.id,
                    Mandate.status.in_(
                        [
                            "CREATED",
                            "APPROVED",
                            "AUTHORIZED",
                            "ACTIVE",
                            "CLOSING",
                            "CAPTURING",
                        ]
                    ),
                )
            )
            if m and (
                m.cap_micros != cap
                or m.payment_method != ("card" if card else "wallet")
            ):
                raise HTTPException(
                    409, "Close the current mandate before changing its budget"
                )
            if m is None:
                m = Mandate(
                    workspace_id=ws,
                    consumer_id=consumer.id,
                    cap_micros=cap,
                    payment_method="card" if card else "wallet",
                )
                db.add(m)
                db.flush()
            if m.paypal_order_id:
                return view(m)
            op = prepare(
                db, ws, "order", m.id, {"cents": cap // 10000, "card": card}, "create"
            )
        page = value("PUBLIC_WEB_URL", "http://127.0.0.1:3000").rstrip("/") + "/agent"
        remote_id = call_once(
            op,
            lambda: pp.create_order(
                cap // 10000,
                op.id,
                card=card,
                return_url=page,
                cancel_url=page + "?cancel=1",
            ),
        )
        order = pp.get_order(remote_id)
        units = order.get("purchase_units", [])
        if (
            order.get("intent") != "AUTHORIZE"
            or len(units) != 1
            or cents(units[0]["amount"]) * 10000 != cap
        ):
            raise ValueError("PayPal order does not match the authorized budget")
        with session_scope() as db:
            obj = owned(db, Mandate, m.id, ws)
            obj.paypal_order_id = remote_id
            db.get(Operation, op.id).result = {"id": remote_id}
            return view(obj)


def authorize(ws, identifier):
    with lock(ws), PayPalClient.from_env() as pp:
        with session_scope() as db:
            m = owned(db, Mandate, identifier, ws)
            if m.authorization_id:
                return view(m)
            if m.status not in {"CREATED", "APPROVED"}:
                raise HTTPException(409, "This mandate is closed to new approvals")
            if not m.paypal_order_id:
                raise HTTPException(409, "Create the sandbox order first")
        order = pp.get_order(m.paypal_order_id)

        def auths(order):
            return [
                a
                for unit in order.get("purchase_units", [])
                for a in unit.get("payments", {}).get("authorizations", [])
            ]

        items = auths(order)
        if (
            not items
            and order.get("status") != "APPROVED"
            and not (m.payment_method == "card" and order.get("status") == "CREATED")
        ):
            raise HTTPException(409, "PayPal buyer approval is required")
        with session_scope() as db:
            op = prepare(
                db, ws, "authorize", m.id, {"order": m.paypal_order_id}, "approve"
            )
        if not items:
            response = pp.authorize_order(m.paypal_order_id, op.id)
            items = auths(response) or auths(pp.get_order(m.paypal_order_id))
        if len(items) != 1:
            raise ValueError("The order does not have exactly one PayPal authorization")
        auth = pp.get_authorization(items[0]["id"])
        if auth["status"] != "CREATED" or cents(auth["amount"]) * 10000 != m.cap_micros:
            raise HTTPException(
                409, "PayPal authorization is not available for this budget"
            )
        with session_scope() as db:
            m = owned(db, Mandate, identifier, ws)
            if m.authorization_id:
                return view(m)
            if m.status == "CREATED":
                transition(m, "APPROVED")
            transition(m, "AUTHORIZED")
            transition(m, "ACTIVE")
            m.authorization_id = auth["id"]
            m.expires_at = utcnow() + timedelta(hours=72)
            remember_authorized_payer(db, m, order)
            db.get(Operation, op.id).result = {"id": auth["id"]}
            return view(m)


def ensure_payer_link(ws, identifier):
    """Backfill an existing mandate using a read-only, verified sandbox order lookup."""
    with session_scope(write=False) as db:
        m = owned(db, Mandate, identifier, ws)
        if db.get(Control, payer_key(m)):
            return
        order_id, authorization = m.paypal_order_id, m.authorization_id
    if not order_id or not authorization:
        raise ValueError("The mandate needs a confirmed PayPal authorization")
    with PayPalClient.from_env() as pp:
        order = pp.get_order(order_id)
    related = {
        a["id"]
        for u in order.get("purchase_units", [])
        for a in u.get("payments", {}).get("authorizations", [])
    }
    if authorization not in related:
        raise ValueError("PayPal order did not confirm this mandate's authorization")
    with session_scope() as db:
        remember_authorized_payer(db, owned(db, Mandate, identifier, ws), order)


def capture(ws, identifier, *, final=False, force=False):
    with lock(ws), PayPalClient.from_env() as pp:
        with session_scope() as db:
            m = owned(db, Mandate, identifier, ws)
            if m.status not in {"ACTIVE", "CAPTURING", "CLOSING"}:
                raise HTTPException(409, "Mandate is not active")
            old = db.scalar(
                select(Operation).where(
                    Operation.subject == m.id,
                    Operation.kind == "capture",
                    Operation.result.is_(None),
                )
            )
            if old:
                op = old
                amount = op.payload["amount"]
                final = op.payload["final"]
            else:
                judge = db.get(Workspace, ws).kind == "judge"
                threshold = usd_to_micros(
                    value("JUDGE_CAPTURE_THRESHOLD_USD", ".02")
                    if judge
                    else value("CAPTURE_THRESHOLD_USD", "2")
                )
                plan = plan_capture(
                    m.accrued_uncaptured_micros,
                    m.cap_micros - m.captured_micros,
                    threshold,
                    final=final,
                    force=force,
                    allow_partial=value("PAYPAL_CAPTURE_POLICY", "partial")
                    == "partial",
                )
                amount = plan.amount_cents
                if not amount:
                    return None
                op = prepare(
                    db,
                    ws,
                    "capture",
                    m.id,
                    {"amount": amount, "final": final},
                    m.captured_micros,
                )
                if m.status == "ACTIVE":
                    transition(m, "CAPTURING")
        remote_id = call_once(
            op, lambda: pp.capture(m.authorization_id, amount, final, op.id)
        )
        remote = pp.get_capture(remote_id)
        if remote["status"] != "COMPLETED":
            raise HTTPException(409, f"PayPal capture is {remote['status']}")
        parent = (
            remote.get("supplementary_data", {})
            .get("related_ids", {})
            .get("authorization_id")
        )
        if parent and parent != m.authorization_id:
            raise ValueError("Capture is tied to a different authorization")
        if remote["id"] != remote_id or remote.get("invoice_id") != op.id or cents(remote["amount"]) != amount:
            raise ValueError("Capture identity or amount differs from its prepared request")
        if remote.get("final_capture") is not final:
            disposition = pp.get_authorization(m.authorization_id)["status"]
            if not capture_flag_matches(final, remote.get("final_capture"), disposition):
                raise ValueError("Capture flag differs from its request and authorization disposition")
        fee = cents(remote["seller_receivable_breakdown"]["paypal_fee"])
        with session_scope() as db:
            m = owned(db, Mandate, identifier, ws)
            row = db.scalar(
                select(Capture).where(Capture.paypal_capture_id == remote_id)
            )
            if row is None:
                row = Capture(
                    workspace_id=ws,
                    mandate_id=m.id,
                    paypal_capture_id=remote_id,
                    final=final,
                    amount_cents=amount,
                    fee_cents=fee,
                    status="COMPLETED",
                )
                ledger.record_capture(db, row)
            if m.status == "CAPTURING":
                transition(m, "ACTIVE")
            db.get(Operation, op.id).result = {"id": remote_id}
            return {
                "id": row.id,
                "paypal_capture_id": remote_id,
                "amount_cents": amount,
                "fee_cents": fee,
            }


def close(ws, identifier):
    with lock(ws), PayPalClient.from_env() as pp:
        with session_scope() as db:
            m = owned(db, Mandate, identifier, ws)
            if m.status in {"CLOSED", "VOIDED", "EXPIRED", "FAILED"}:
                return view(m)
            active = db.scalar(
                select(AgentRun.id).where(
                    AgentRun.mandate_id == m.id,
                    AgentRun.status.in_(["running", "queued"]),
                )
            )
            if active:
                raise HTTPException(
                    409, "Wait until the agent run finishes before closing"
                )
            if not m.authorization_id:
                pending = db.scalar(
                    select(Operation.id).where(
                        Operation.subject == m.id, Operation.kind == "authorize"
                    )
                )
                if pending or not m.paypal_order_id:
                    raise HTTPException(
                        409, "Recover the pending PayPal operation before closing"
                    )
            if m.status == "CAPTURING":
                raise HTTPException(409, "A prior capture is awaiting confirmation")
            if m.authorization_id and m.status != "CLOSING":
                transition(m, "CLOSING")
        if not m.authorization_id:
            order = pp.get_order(m.paypal_order_id)
            has_payments = any(
                unit.get("payments") for unit in order.get("purchase_units", [])
            )
            if (
                order.get("id") != m.paypal_order_id
                or has_payments
                or order.get("status")
                not in {
                    "CREATED",
                    "APPROVED",
                    "VOIDED",
                    "PAYER_ACTION_REQUIRED",
                    "SAVED",
                }
            ):
                raise HTTPException(
                    409, "Read back the PayPal authorization before closing"
                )
            with session_scope() as db:
                obj = owned(db, Mandate, identifier, ws)
                transition(obj, "FAILED")
                return view(obj)
        capture(ws, identifier, final=True)
        auth = pp.get_authorization(m.authorization_id)
        if auth["status"] in {"CREATED", "PARTIALLY_CAPTURED"}:
            pp.void(m.authorization_id, request_id(ws, "void", m.id))
            auth = pp.get_authorization(m.authorization_id)
        if auth["id"] != m.authorization_id or cents(auth["amount"]) * 10000 != m.cap_micros:
            raise ValueError("Release readback differs from this mandate's authorization")
        if auth["status"] not in {"VOIDED", "CAPTURED", "EXPIRED"}:
            raise HTTPException(409, f"PayPal authorization is still {auth['status']}")
        with session_scope() as db:
            m = owned(db, Mandate, identifier, ws)
            ledger.close_dust(db, m)
            transition(
                m,
                "CLOSED"
                if m.captured_micros
                else ("EXPIRED" if auth["status"] == "EXPIRED" else "VOIDED"),
            )
            return view(m)


def refund_calls(ws, capture_id, call_ids, reason):
    with lock(ws), PayPalClient.from_env() as pp:
        with session_scope() as db:
            cap = owned(db, Capture, capture_id, ws)
            key = ",".join(sorted(call_ids))
            op_id = request_id(ws, "refund", cap.id, reason + key)
            op = db.get(Operation, op_id)
            if op and op.result:
                return op.result
            if op is None:
                plan = ledger.plan_refund(db, cap, call_ids, reason=reason)
                if not plan.amount_cents:
                    return ledger.apply_refund_credit(db, cap, call_ids, reason=reason)
                op = prepare(
                    db,
                    ws,
                    "refund",
                    cap.id,
                    dict(
                        amount=plan.amount_cents, calls=sorted(call_ids), reason=reason
                    ),
                    reason + key,
                )
            amount = op.payload["amount"]
        remote_id = call_once(
            op, lambda: pp.refund(cap.paypal_capture_id, amount, op.id)
        )
        r = pp.get_refund(remote_id)
        if r["status"] != "COMPLETED":
            raise HTTPException(409, f"PayPal refund is {r['status']}")
        if cents(r["amount"]) != amount:
            raise ValueError("Refund readback amount differs")
        fee = r.get("seller_payable_breakdown", {}).get("paypal_fee")
        with session_scope() as db:
            row = db.scalar(select(Refund).where(Refund.paypal_refund_id == remote_id))
            if row and (
                row.workspace_id != ws
                or row.capture_id != cap.id
                or row.amount_cents != amount
                or sorted(row.call_ids_json) != sorted(call_ids)
            ):
                raise ValueError("Confirmed refund belongs to a different operation")
            if row is None:
                row = Refund(
                    workspace_id=ws,
                    capture_id=cap.id,
                    paypal_refund_id=remote_id,
                    amount_cents=amount,
                    fee_reversal_cents=abs(cents(fee)) if fee else 0,
                    status="COMPLETED",
                    reason=reason,
                    call_ids_json=sorted(call_ids),
                )
                ledger.record_refund(db, row)
            result = {
                "id": row.id,
                "paypal_refund_id": remote_id,
                "amount_cents": amount,
            }
            db.get(Operation, op.id).result = result
            return result
