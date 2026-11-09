"""Read-only evidence for the workspace agent and explicit, scoped refund proposals."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import ledger
from ..db import session_scope
from ..models import (
    AgentRun, Call, Capture, LedgerEntry, Mandate, PayoutBatch, PayoutItem, Publisher, Refund, utcnow,
)
from ..operations import Control
from ..paypal import toolkit
from ..paypal.client import PayPalConfigError, PayPalError
from ..settlement.audit import audit
from ..settlement.service import lock
from .anomaly import activity


def usd(micros):
    return f"${Decimal(micros) / 1_000_000:,.6f}"


def count(db, model, ws):
    return db.scalar(select(func.count()).select_from(model).where(model.workspace_id == ws))


def overview(db, ws):
    rows = {m.__name__: count(db, m, ws) for m in (Publisher, Mandate, Call, Capture, Refund, PayoutBatch)}
    lines = ["This workspace contains " + ", ".join(f"{n} {k.lower()} records" for k, n in rows.items()) + "."]
    for cls in (Mandate, Call, Capture, Refund, PayoutBatch):
        ids = list(db.scalars(select(cls.id).where(cls.workspace_id == ws).order_by(cls.created_at.desc()).limit(3)))
        if ids:
            lines.append(f"Most recent {cls.__name__.lower()} IDs: {', '.join(ids)}.")
    return lines


def earnings(db, ws, days=7):
    if type(days) is not int or not 1 <= days <= 365:
        raise ValueError("The earnings window must be 1 to 365 days.")
    since = utcnow() - timedelta(days=days)
    pubs = {p.id: p for p in db.scalars(select(Publisher).where(Publisher.workspace_id == ws))}
    if not pubs:
        return ["There are no publishers in this workspace, so it has no publisher earnings to compare."]
    stats = {pid: dict(hits=0, gross=0, linear=0, call=None) for pid in pubs}
    window = [Call.workspace_id == ws, Call.created_at >= since]
    for c in db.scalars(select(Call).where(*window).order_by(Call.created_at)):
        for part in c.spans_json:
            if part["kind"] == "publisher" and part["owner"] in stats:
                pid = part["owner"]
                n = max(0, min(part["end"], c.cached_tokens) - part["start"])
                stats[pid]["hits"] += n
                if n:
                    stats[pid]["call"] = c.id
        for pid in c.royalty_json:
            if pid in stats:
                stats[pid]["gross"] += c.royalty_json[pid]
                stats[pid]["linear"] += c.linear_royalty_json.get(pid, 0)
    lines = []
    for pid, p in pubs.items():
        s = stats[pid]
        net = db.scalar(select(func.coalesce(func.sum(LedgerEntry.credit_micros - LedgerEntry.debit_micros), 0))
                        .where(LedgerEntry.workspace_id == ws, LedgerEntry.created_at >= since,
                               LedgerEntry.account == ledger.payable(pid), LedgerEntry.ref_type != "payout"))
        latest = f" Last contributing call: {s['call']}." if s["call"] else ""
        lines.append(f"{p.name} (publisher {pid}) earned {usd(net)} net in the last {days} days, "
                     f"from {s['hits']:,} measured prefix hits and {usd(s['gross'])} gross royalties.{latest}")
        if s["hits"]:
            rate = Decimal(s["gross"]) / s["hits"]
            lines.append(f"{p.name} received {rate:.6f} microdollars per hit before reversals. "
                         f"A linear split on the same calls gives {usd(s['linear'])}.")
    if any(s["gross"] != s["linear"] for s in stats.values()):
        lines.append("The Shapley split reflects ordered dependencies. Later layers can save prefill work only "
                     "after their ancestors have matched, increasing the ancestors' marginal contribution.")
    return lines


def scoped(db, model, ws, identifier):
    obj = db.get(model, identifier)
    return obj if obj is not None and obj.workspace_id == ws else None


def mandate_breakdown(db, ws, mandate_id):
    m = scoped(db, Mandate, ws, mandate_id)
    if not m:
        return ["No matching mandate was found in this workspace."]
    lines = [f"Mandate {m.id} is {m.status}. Its cap is {usd(m.cap_micros)}, captured "
             f"{usd(m.captured_micros)}, pending {usd(m.accrued_uncaptured_micros)}, refunded {usd(m.refunded_micros)}."]
    if m.authorization_id:
        lines.append(f"Sandbox order {m.paypal_order_id} issued authorization {m.authorization_id}.")
    if m.status in ("CLOSED", "VOIDED", "EXPIRED"):
        lines.append(f"The remaining authorization was {usd(m.cap_micros - m.captured_micros)} at close.")
    royalty = platform = 0
    for c in db.scalars(select(Call).join(AgentRun, AgentRun.id == Call.run_id).where(AgentRun.mandate_id == m.id)):
        royalty += sum(c.royalty_json.values())
        platform += c.platform_micros
    lines.append(f"Metered usage accrued {usd(royalty)} to publishers and {usd(platform)} to the platform before reversals.")
    caps = list(db.scalars(select(Capture).where(Capture.mandate_id == m.id).order_by(Capture.created_at)))
    for cap in caps:
        lines.append(f"Capture {cap.paypal_capture_id or cap.id} is {cap.status}: {usd(cap.amount_cents * 10000)} with "
                     f"{usd(cap.fee_cents * 10000)} in recorded PayPal fees.")
        for r in db.scalars(select(Refund).where(Refund.capture_id == cap.id)):
            lines.append(f"Refund {r.paypal_refund_id or r.id} for capture {cap.paypal_capture_id} is {r.status}: "
                         f"{usd(r.amount_cents * 10000)} for {r.reason}.")
    return lines


def call_details(db, ws, call_id):
    c = scoped(db, Call, ws, call_id)
    if not c:
        return ["No matching metered call was found in this workspace."]
    lines = [f"Call {c.id} ({c.mode}) measured {c.cached_tokens:,}/{c.prompt_tokens:,} cached input tokens "
             f"and {c.output_tokens:,} output tokens. First text arrived in {c.ttft_ms:,.1f} ms.",
             f"The call charge is {usd(c.charge_micros)}. Public hit fees are {usd(c.public_charge_micros)} "
             f"and platform revenue before reversals is {usd(c.platform_micros)}."]
    for pid, n in c.royalty_json.items():
        p = scoped(db, Publisher, ws, pid)
        if p:
            lines.append(f"{p.name} ({pid}) was allocated {usd(n)}; a linear split gives "
                         f"{usd(c.linear_royalty_json.get(pid, 0))}.")
    for span in c.spans_json:
        if span.get("self_royalty_suppressed"):
            lines.append(f"Call {c.id} hit {span['hits']:,} tokens in {span['slug']}. Its verified buyer "
                         "also receives this publisher's payments, so the call earned zero royalties on that layer.")
    if c.sla_breach:
        lines.append("This call exceeded the warm-latency SLA.")
    if c.fault_injected_ms:
        lines.append(f"Fault injection (test) added {c.fault_injected_ms:g} ms.")
    return lines


def payment_activity(db, ws):
    lines = []
    for cap in db.scalars(select(Capture).where(Capture.workspace_id == ws).order_by(Capture.created_at.desc()).limit(8)):
        lines.append(f"Capture {cap.paypal_capture_id or cap.id} (mandate {cap.mandate_id}) is {cap.status} for {usd(cap.amount_cents * 10000)}.")
    for r in db.scalars(select(Refund).where(Refund.workspace_id == ws).order_by(Refund.created_at.desc()).limit(8)):
        lines.append(f"Refund {r.paypal_refund_id or r.id} (local capture {r.capture_id}) is {r.status} for {usd(r.amount_cents * 10000)}.")
    for i in db.scalars(select(PayoutItem).where(PayoutItem.workspace_id == ws).order_by(PayoutItem.created_at.desc()).limit(8)):
        lines.append(f"Payout item {i.paypal_item_id or i.id} (publisher {i.publisher_id}, batch {i.batch_id}) "
                     f"is {i.status} for {usd(i.amount_cents * 10000)}.")
    return lines or ["No captures, refunds or payout items have been recorded in this workspace."]


def ledger_activity(db, ws):
    entries = db.scalars(select(LedgerEntry).where(LedgerEntry.workspace_id == ws)
                         .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id).limit(12))
    return [f"Transaction {r.txn_id}: {r.account} {'debit' if r.debit_micros else 'credit'} "
            f"{usd(r.debit_micros or r.credit_micros)}, from {r.ref_type} {r.ref_id}." for r in entries] or [
                "The workspace ledger contains no posted entries."]


def anomaly_signals(db, ws):
    lines = []
    for s in activity(db, ws):
        if s["self_use_hits"]:
            lines.append(f"{s['name']} had {s['self_use_hits']:,} same-payer hits today. These hits earned zero royalties.")
        z = s["z_score"]
        if z is None:
            result = "A z-score requires seven comparable historical days with nonzero variance."
        else:
            result = f"The same-time seven-day hit z-score is {z:.2f}. " + ("This exceeds the 3σ spike threshold." if z > 3 else "It is within the spike threshold.")
        lines.append(f"{s['name']} ({s['publisher_id']}) has {s['today_hits']:,} measured hits today in UTC. {result}")
    return lines or ["There are no publisher measurements to assess in this workspace."]


LOCAL = {
    "workspace_overview": overview, "publisher_earnings": earnings, "mandate_breakdown": mandate_breakdown,
    "call_details": call_details, "payment_activity": payment_activity, "ledger_activity": ledger_activity,
    "anomaly_signals": anomaly_signals,
}


def paypal_read(ws, name, arguments):
    with session_scope(write=False) as db:
        order_ids = list(db.scalars(select(Mandate.paypal_order_id).where(
            Mandate.workspace_id == ws, Mandate.paypal_order_id.is_not(None))))
        transaction_ids = []
        for model, col in [(Capture, Capture.paypal_capture_id), (Refund, Refund.paypal_refund_id),
                           (PayoutItem, PayoutItem.paypal_item_id)]:
            transaction_ids.extend(db.scalars(select(col).where(model.workspace_id == ws, col.is_not(None))))
    if name == "get_order_details" and not order_ids:
        return ["No PayPal order is attached to this workspace."]
    if name == "list_transactions" and not transaction_ids:
        return ["This workspace has no confirmed PayPal transaction IDs to query."]
    try:
        data = toolkit.run_tool(name, arguments, order_ids=order_ids, transaction_ids=transaction_ids)
    except (PayPalConfigError, PayPalError, ValueError) as exc:
        return [f"The sandbox lookup was unavailable: {exc}"]
    if name == "get_order_details":
        amounts = ", ".join(f"{a['currency_code']} {a['value']}" for a in data['amounts'])
        return [f"PayPal Agent Toolkit returned order {data['id']}, intent {data['intent']}, "
                f"status {data['status']}, amount {amounts}."]
    return [f"PayPal Agent Toolkit returned transaction {r['transaction_id']}, status {r.get('transaction_status')}, "
            f"amount {r.get('transaction_amount', {}).get('currency_code')} {r.get('transaction_amount', {}).get('value')}."
            for r in data['transactions']] or ["PayPal returned no matching transactions for this workspace and reporting window."]


def propose(ws, call_id):
    with session_scope() as db:
        c = scoped(db, Call, ws, call_id)
        if c is None:
            return ["No matching metered call was found in this workspace."], None
        m = db.get(AgentRun, c.run_id).mandate_id
        if not c.charge_micros:
            return [f"Call {c.id} was free. It has no charge to refund."], None
        identifier = uuid4().hex
        description = (f"Review the dispute for call {c.id} on mandate {m}. The original charge was "
                       f"{usd(c.charge_micros)}. Confirmation reverses eligible pending charges and requests "
                       "eligible captured refunds in the PayPal sandbox. Prior refunds and cent rounding apply.")
        data = dict(proposal_id=identifier, call_id=c.id, mandate_id=m, description=description,
                    status="pending", expires_at=(utcnow() + timedelta(minutes=15)).timestamp())
        db.add(Control(key=f"refund-proposal:{ws}:{identifier}", data=data))
        return [], {k: data[k] for k in ("proposal_id", "call_id", "mandate_id", "description")}


def confirm(ws, identifier):
    key = f"refund-proposal:{ws}:{identifier}"
    with lock(ws):
        with session_scope() as db:
            row = db.get(Control, key)
            if row is None:
                raise HTTPException(404, "Refund proposal was not found in this workspace.")
            data = row.data.copy()
            if data["status"] == "completed":
                return data["receipt"]
            now = utcnow().timestamp()
            if data["status"] == "pending" and data["expires_at"] < now:
                raise HTTPException(410, "Refund proposal expired. Request a new review.")
            if data["status"] == "confirming" and now - data["started_at"] < 120:
                raise HTTPException(409, "Refund confirmation is already in progress.")
            row.data = {**data, "status": "confirming", "started_at": now}
        try:
            result = audit(ws, data["mandate_id"], data["call_id"])
        except Exception:
            with session_scope() as db:
                row = db.get(Control, key)
                row.data = {**row.data, "status": "pending"}
            raise
        receipt = {"proposal_id": identifier, "call_id": data["call_id"], **result}
        with session_scope() as db:
            # A retry may follow a committed reversal and a lost HTTP response.
            # Report its durable cumulative total separately from this invocation.
            total = db.scalar(select(func.coalesce(func.sum(LedgerEntry.credit_micros), 0)).where(
                LedgerEntry.workspace_id == ws, LedgerEntry.ref_type == "uncaptured_dispute",
                LedgerEntry.ref_id == data["call_id"], LedgerEntry.account.like("consumer:%:receivable")))
            receipt["total_dispute_reversal_micros"] = total
            db.get(Control, key).data = {**data, "status": "completed", "receipt": receipt}
        return receipt
