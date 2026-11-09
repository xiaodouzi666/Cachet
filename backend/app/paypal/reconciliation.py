"""Reconcile one workspace against GET responses from its sandbox app."""

from collections import defaultdict
from sqlalchemy import select

from backend.app import ledger
from backend.app.db import session_scope
from backend.app.models import Capture, Mandate, PayoutBatch, PayoutItem, Publisher, Refund
from backend.app.paypal.client import PayPalClient, PayPalError
from backend.app.settlement.service import cents, lock


class Report:
    def __init__(self):
        self.checked = 0
        self.pending = []
        self.differences = []
        self.clearing = 0

    def compare(self, local_id, field, local, remote):
        if local != remote:
            self.differences.append({"id":local_id, "field":field, "local":local, "paypal":remote})

    def fetch(self, local, field, provider_id, fn):
        if not provider_id:
            self.pending.append({"id":local.id, "field":field})
            return None
        try:
            result = fn(provider_id)
        except PayPalError as exc:
            if exc.status_code != 404:
                raise
            self.differences.append({"id":local.id, "field":field, "detail":"Resource missing in PayPal Sandbox"})
            return None
        self.checked += 1
        return result

    def output(self):
        if self.differences:
            status = "differences"
        elif self.pending:
            status = "pending"
        elif self.checked:
            status = "matched"
        else:
            status = "empty"
        return {"ok":True if status == "matched" else False if status == "differences" else None, "status":status,
                "checked":self.checked, "differences":self.differences, "pending":self.pending,
                "clearing_micros":self.clearing * 10000 if self.checked else None}


def orders(pp, report, mandates, captures):
    for m in mandates:
        order = report.fetch(m, "order", m.paypal_order_id, pp.get_order)
        if not order:
            continue
        report.compare(m.id, "order ID", m.paypal_order_id, order["id"])
        report.compare(m.id, "order intent", "AUTHORIZE", order["intent"])
        units = order["purchase_units"]
        report.compare(m.id, "purchase units", 1, len(units))
        report.compare(m.id, "order amount", m.cap_micros, sum(cents(u["amount"]) for u in units) * 10000)
        linked = sorted(a["id"] for u in units for a in u.get("payments", {}).get("authorizations", []))
        report.compare(m.id, "order authorizations", [m.authorization_id] if m.authorization_id else [], linked)
        local_captures = {c.paypal_capture_id for c in captures if c.mandate_id == m.id}
        provider_captures = {c["id"] for u in units for c in u.get("payments", {}).get("captures", [])}
        report.compare(m.id, "unrecognized order captures", [], sorted(provider_captures - local_captures))
        if not m.authorization_id and m.status in {"CREATED", "APPROVED", "FAILED"}:
            continue
        auth = report.fetch(m, "authorization", m.authorization_id, pp.get_authorization)
        if auth:
            report.compare(m.id, "authorization ID", m.authorization_id, auth["id"])
            report.compare(m.id, "authorization amount", m.cap_micros, cents(auth["amount"]) * 10000)
            if m.status in {"CLOSED", "VOIDED", "EXPIRED"}:
                disposition = {"CAPTURED", "VOIDED", "EXPIRED"} if m.status == "CLOSED" else {m.status}
                if auth["status"] not in disposition:
                    report.compare(m.id, "authorization status", m.status, auth["status"])
            if m.status == "ACTIVE" and auth["status"] not in {"CREATED", "PARTIALLY_CAPTURED"}:
                report.compare(m.id, "authorization status", m.status, auth["status"])


def cash(pp, report, captures, refunds):
    paid_back = defaultdict(int)
    for f in refunds:
        remote = report.fetch(f, "refund", f.paypal_refund_id, pp.get_refund)
        if not remote:
            continue
        amount = cents(remote["amount"])
        fee = (remote.get("seller_payable_breakdown") or {}).get("paypal_fee")
        reversal = abs(cents(fee)) if fee else 0
        report.compare(f.id, "refund ID", f.paypal_refund_id, remote["id"])
        report.compare(f.id, "refund amount", f.amount_cents, amount)
        report.compare(f.id, "refund fee reversal", f.fee_reversal_cents, reversal)
        report.compare(f.id, "refund status", f.status, remote["status"])
        if remote["status"] == "COMPLETED":
            paid_back[f.capture_id] += amount
            report.clearing -= amount - reversal
    for cap in captures:
        remote = report.fetch(cap, "capture", cap.paypal_capture_id, pp.get_capture)
        if not remote:
            continue
        amount = cents(remote["amount"])
        report.compare(cap.id, "capture ID", cap.paypal_capture_id, remote["id"])
        report.compare(cap.id, "capture amount", cap.amount_cents, amount)
        report.compare(cap.id, "final capture", cap.final, remote.get("final_capture"))
        refunded = paid_back[cap.id]
        status = "REFUNDED" if amount and refunded == amount else "PARTIALLY_REFUNDED" if refunded else "COMPLETED"
        report.compare(cap.id, "capture status after known refunds", status, remote["status"])
        if remote["status"] in ledger.CAPTURED:
            fee = cents(remote["seller_receivable_breakdown"]["paypal_fee"])
            report.compare(cap.id, "capture fee", cap.fee_cents, fee)
            report.clearing += amount - fee


def payouts(pp, report, batches, items, receivers):
    provider_batches = {b.id:b.paypal_batch_id for b in batches}
    for b in batches:
        remote = report.fetch(b, "payout batch", b.paypal_batch_id, pp.get_payout)
        if not remote:
            continue
        header = remote["batch_header"]
        report.compare(b.id, "payout batch ID", b.paypal_batch_id, header["payout_batch_id"])
        report.compare(b.id, "sender batch ID", b.sender_batch_id, header["sender_batch_header"]["sender_batch_id"])
        report.compare(b.id, "batch status", b.status, header["batch_status"])
        if header["batch_status"] == "SUCCESS":
            local = sorted(i.id for i in items if i.batch_id == b.id)
            report.compare(b.id, "sender payout items", local, sorted(i["payout_item"]["sender_item_id"] for i in remote["items"]))
            if header.get("fees"):
                report.compare(b.id, "batch fee", b.fee_cents, cents(header["fees"]))
    for item in items:
        remote = report.fetch(item, "payout item", item.paypal_item_id, pp.get_payout_item)
        if not remote:
            continue
        payload = remote["payout_item"]
        report.compare(item.id, "payout ID", item.paypal_item_id, remote["payout_item_id"])
        report.compare(item.id, "payout batch ID", provider_batches[item.batch_id], remote["payout_batch_id"])
        report.compare(item.id, "sender item ID", item.id, payload.get("sender_item_id"))
        report.compare(item.id, "receiver matches publisher", True,
                       str(payload.get("receiver", "")).casefold() == receivers[item.publisher_id].casefold())
        amount = cents(payload["amount"])
        report.compare(item.id, "payout amount", item.amount_cents, amount)
        report.compare(item.id, "payout status", item.status, remote["transaction_status"])
        if remote["transaction_status"] == "SUCCESS":
            fee = cents(remote["payout_item_fee"])
            report.compare(item.id, "payout fee", item.fee_cents, fee)
            report.clearing -= amount + fee


def reconcile(ws):
    report = Report()
    with lock(ws):
        with session_scope(write=False) as db:
            def scoped(model):
                return list(db.scalars(select(model).where(model.workspace_id == ws)))
            mandates, captures, refunds, batches, items = map(scoped, (Mandate, Capture, Refund, PayoutBatch, PayoutItem))
            receivers = {p.id:p.paypal_receiver_email for p in scoped(Publisher)}
        if any((mandates, captures, refunds, batches, items)):
            with PayPalClient.from_env() as pp:
                orders(pp, report, mandates, captures)
                cash(pp, report, captures, refunds)
                payouts(pp, report, batches, items, receivers)
        with session_scope(write=False) as db:
            try:
                ledger.assert_invariants(db, workspace_id=ws, expected_clearing_micros=report.clearing * 10000)
            except ledger.LedgerError as exc:
                report.differences.append({"field":"ledger", "detail":str(exc)})
    return report.output()
