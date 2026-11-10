from datetime import timedelta

from sqlalchemy import select
from backend.app import ledger
from backend.app.api.access import owned
from backend.app.config import value
from backend.app.db import session_scope
from backend.app.models import (
    PayoutBatch,
    PayoutItem,
    Publisher,
    Workspace,
    new_id,
    utcnow,
)
from backend.app.money import usd_to_micros
from backend.app.paypal.client import PayPalClient, request_id
from backend.app.settlement.service import cents, lock


def refresh_batch(pp, ws, batch_id):
    with session_scope(write=False) as db:
        b = owned(db, PayoutBatch, batch_id, ws)
    info = pp.get_payout(b.paypal_batch_id)
    hdr = info["batch_header"]
    if (
        hdr["payout_batch_id"] != b.paypal_batch_id
        or hdr["sender_batch_header"]["sender_batch_id"] != b.sender_batch_id
    ):
        raise ValueError("PayPal batch identity differs")
    with session_scope() as db:
        b = db.get(PayoutBatch, batch_id)
        for row in info.get("items", []):
            p = row["payout_item"]
            item = db.get(PayoutItem, p["sender_item_id"])
            if (
                item is None
                or item.batch_id != b.id
                or cents(p["amount"]) != item.amount_cents
                or p["receiver"].casefold()
                != db.get(Publisher, item.publisher_id).paypal_receiver_email.casefold()
                or (
                    item.paypal_item_id and item.paypal_item_id != row["payout_item_id"]
                )
            ):
                raise ValueError("PayPal payout readback differs from local allocation")
            if item.status == "SUCCESS" and row["transaction_status"] != "SUCCESS":
                raise ValueError(
                    "Previously paid item changed status and requires reconciliation"
                )
            item.paypal_item_id = row["payout_item_id"]
            item.status = row["transaction_status"]
            if item.status == "SUCCESS":
                ledger.record_payout(db, item, fee_cents=cents(row["payout_item_fee"]))
        b.status = hdr["batch_status"]
        return {
            "id": b.id,
            "paypal_batch_id": b.paypal_batch_id,
            "status": b.status,
            "fee_cents": b.fee_cents,
        }


def settle(ws, batch_id=None):
    with lock(ws):
        with session_scope() as db:
            b = (
                owned(db, PayoutBatch, batch_id, ws)
                if batch_id
                else db.scalar(
                    select(PayoutBatch)
                    .where(
                        PayoutBatch.workspace_id == ws,
                        PayoutBatch.status.in_(["CREATED", "PENDING", "PROCESSING"]),
                    )
                    .order_by(PayoutBatch.created_at)
                )
            )
            if b is None:
                judge = db.get(Workspace, ws).kind == "judge"
                minimum = usd_to_micros(
                    value("JUDGE_PAYOUT_MIN_USD", ".01")
                    if judge
                    else value("PAYOUT_MIN_USD", ".25")
                )
                owing = [
                    (p, ledger.available_payable(db, ws, p.id))
                    for p in db.scalars(
                        select(Publisher).where(Publisher.workspace_id == ws)
                    )
                ]
                payable = [
                    (p, amt // 10000)
                    for p, amt in owing
                    if amt >= minimum and amt >= 10000
                ]
                if not payable:
                    return {"status": "below_minimum", "items": []}
                b = PayoutBatch(
                    workspace_id=ws, sender_batch_id=new_id(), status="CREATED"
                )
                db.add(b)
                db.flush()
                for p, amount in payable:
                    db.add(
                        PayoutItem(
                            workspace_id=ws,
                            batch_id=b.id,
                            publisher_id=p.id,
                            amount_cents=amount,
                            status="PENDING",
                        )
                    )
                db.flush()
            payout_items = list(
                db.scalars(select(PayoutItem).where(PayoutItem.batch_id == b.id))
            )
            if not b.paypal_batch_id:
                if utcnow() - b.created_at >= timedelta(days=29):
                    raise ValueError(
                        "Unconfirmed payout is near the sender-batch deduplication limit; reconcile its PayPal status"
                    )
                for item in payout_items:
                    available = ledger.available_payable(
                        db, ws, item.publisher_id, excluding_reservation=item.id
                    )
                    if item.amount_cents * 10_000 > available:
                        raise ValueError(
                            "The prepared payout exceeds funded royalties; reconcile before resubmission"
                        )
            items = [
                (
                    i.id,
                    db.get(Publisher, i.publisher_id).paypal_receiver_email,
                    i.amount_cents,
                )
                for i in payout_items
            ]
        with PayPalClient.from_env() as pp:
            if not b.paypal_batch_id:
                reply = pp.create_payout(
                    b.sender_batch_id, items, request_id("settle", b.id)
                )
                batch = reply["batch_header"]["payout_batch_id"]
                with session_scope() as db:
                    rec = db.get(PayoutBatch, b.id)
                    rec.paypal_batch_id = batch
                    rec.status = "PENDING"
            return refresh_batch(pp, ws, b.id)
