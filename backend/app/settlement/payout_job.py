from datetime import timedelta
import hashlib
import json
import logging

from fastapi import HTTPException

from sqlalchemy import func, literal, select
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
from backend.app.operations import Control
from backend.app.money import usd_to_micros
from backend.app.paypal.client import PayPalClient, request_id
from backend.app.settlement.service import cents, lock


LOG = logging.getLogger(__name__)
POLL_PREFIX = "payout-readback:"
OPEN_BATCH = ("CREATED", "PENDING", "PROCESSING")
OPEN_ITEM = ("NEW", "PENDING", "PROCESSING", "ONHOLD", "UNCLAIMED")


def refresh_submitted_payouts():
    """Poll four already-submitted batches, oldest attempt first, across all workspaces."""
    now = utcnow().timestamp()
    open_items = select(PayoutItem.id).where(
        PayoutItem.batch_id == PayoutBatch.id, PayoutItem.status.in_(OPEN_ITEM)
    ).exists()
    last = func.coalesce(Control.data["at"].as_float(), 0)
    due = (select(PayoutBatch.workspace_id, PayoutBatch.id)
           .outerjoin(Control, Control.key == literal(POLL_PREFIX) + PayoutBatch.id)
           .where(PayoutBatch.paypal_batch_id.is_not(None), PayoutBatch.paypal_batch_id != "",
                  PayoutBatch.status.in_(OPEN_BATCH) | open_items, last <= now - 30)
           .order_by(last, PayoutBatch.created_at, PayoutBatch.id).limit(4))
    with session_scope(write=False) as db:
        if not db.execute(due).first():
            return
    # BEGIN IMMEDIATE prevents overlapping workers from taking the same due entries.
    # Record the attempt before the GET so an unavailable batch cannot starve later ones.
    with session_scope() as db:
        jobs = db.execute(due).all()
        for _, batch_id in jobs:
            db.merge(Control(key=POLL_PREFIX + batch_id, data={"at": now}))
    if jobs:
        with PayPalClient.from_env() as pp:
            for ws, batch_id in jobs:
                try:
                    with lock(ws):
                        refresh_batch(pp, ws, batch_id)
                except Exception:
                    LOG.exception("Could not refresh submitted payout batch %s", batch_id)


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
        if b.status in ("SUCCESS", "CANCELED", "DENIED") and hdr["batch_status"] != b.status:
            raise ValueError("A PayPal readback differs from the known terminal batch status")
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


def allocation(db, ws, batch_id=None):
    batch = owned(db, PayoutBatch, batch_id, ws) if batch_id else db.scalar(
        select(PayoutBatch).where(PayoutBatch.workspace_id == ws,
            PayoutBatch.status.in_(["CREATED", "PENDING", "PROCESSING"])).order_by(PayoutBatch.created_at))
    judge = db.get(Workspace, ws).kind == "judge"
    minimum = usd_to_micros(value("JUDGE_PAYOUT_MIN_USD", ".01") if judge else value("PAYOUT_MIN_USD", ".25"))
    if batch:
        rows = db.execute(select(Publisher, PayoutItem.amount_cents).join(
            PayoutItem, PayoutItem.publisher_id == Publisher.id
        ).where(PayoutItem.batch_id == batch.id).order_by(Publisher.id)).all()
    else:
        rows = []
        for publisher in db.scalars(select(Publisher).where(Publisher.workspace_id == ws).order_by(Publisher.id)):
            amount = ledger.available_payable(db, ws, publisher.id)
            if amount >= max(minimum, 10000):
                rows.append((publisher, amount // 10000))
    # A retry of an old confirmation must not spend a later, equally-sized accrual.
    last_batch_id = db.scalar(select(PayoutBatch.id).where(PayoutBatch.workspace_id == ws)
                              .order_by(PayoutBatch.created_at.desc(), PayoutBatch.id.desc()).limit(1))
    identity = [ws, batch.id if batch else None, last_batch_id, [(p.id, cents) for p, cents in rows]]
    quote_id = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return batch, rows, {"quote_id": quote_id, "minimum_micros": minimum,
        "batch_id": batch.id if batch else None, "amount_cents": sum(cents for _, cents in rows),
        "items": [{"publisher_id": p.id, "name": p.name, "amount_cents": cents} for p, cents in rows]}


def settle(ws, batch_id=None, *, quote_id=None):
    with lock(ws):
        with session_scope() as db:
            b, payable, preview = allocation(db, ws, batch_id)
            if quote_id is not None and quote_id != preview["quote_id"]:
                raise HTTPException(409, "The funded payout changed. Review the latest amount and confirm again.")
            if b is None:
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
