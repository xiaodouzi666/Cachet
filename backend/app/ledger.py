"""Transactional, double-entry accounting. Positive amounts debit an account."""

from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping

from sqlalchemy import func, select

from .models import (
    AgentRun, Call, Capture, CaptureAllocation, LedgerEntry, LedgerTxn, Mandate,
    PayoutBatch, PayoutItem, Publisher, Refund, utcnow,
)
from .money import MICROS_PER_CENT, allocate, round_cents

CLEARING = "platform:paypal_clearing"
REVENUE = "platform:revenue"
FEES = "platform:fees"
ROUNDING = "platform:rounding"
CAPTURED = ("COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED")
PAID = ("SUCCESS", "SUCCEEDED")


class LedgerError(ValueError):
    pass


def receivable(consumer_id):
    return f"consumer:{consumer_id}:receivable"


def payable(publisher_id):
    return f"publisher:{publisher_id}:payable"


def balance(session, workspace_id, account) -> int:
    return session.scalar(select(func.coalesce(func.sum(LedgerEntry.debit_micros - LedgerEntry.credit_micros), 0))
                          .where(LedgerEntry.workspace_id == workspace_id, LedgerEntry.account == account))


def _txn(session, ref_type, ref_id):
    return session.scalar(select(LedgerTxn).where(LedgerTxn.ref_type == ref_type, LedgerTxn.ref_id == ref_id))


def post(session, workspace_id, ref_type, ref_id, changes: Mapping[str, int]) -> bool:
    """Insert one balanced journal or verify an identical retry within the enclosing transaction."""
    if any(type(n) is not int or not account for account, n in changes.items()) or sum(changes.values()) != 0:
        raise LedgerError("Every journal must balance in integer microdollars")
    expected = {k: v for k, v in changes.items() if v}
    existing = _txn(session, ref_type, ref_id)
    if existing:
        rows = session.scalars(select(LedgerEntry).where(LedgerEntry.txn_id == existing.id))
        if existing.workspace_id != workspace_id or {r.account: r.debit_micros - r.credit_micros for r in rows} != expected:
            raise LedgerError("A previously posted reference has different entries")
        return False
    txn = LedgerTxn(workspace_id=workspace_id, ref_type=ref_type, ref_id=ref_id)
    session.add(txn)
    session.flush()
    for account, delta in expected.items():
        session.add(LedgerEntry(workspace_id=workspace_id, txn_id=txn.id, account=account,
                                debit_micros=max(0, delta), credit_micros=max(0, -delta),
                                ref_type=ref_type, ref_id=ref_id))
    session.flush()
    return True


@dataclass(frozen=True)
class Parts:
    principal: int
    public: int
    royalties: dict[str, int]

    def __post_init__(self):
        if (not 0 <= self.public <= self.principal or any(type(n) is not int or n < 0 for n in self.royalties.values())
                or sum(self.royalties.values()) > self.public):
            raise LedgerError("The charge decomposition is inconsistent")

    def portion(self, amount):
        if not 0 <= amount <= self.principal:
            raise LedgerError("Cannot allocate more than the remaining principal")
        names = sorted(self.royalties)
        weights = [self.principal - self.public, self.public - sum(self.royalties.values())]
        weights.extend(self.royalties[k] for k in names)
        private, _, *royalties = allocate(amount, weights)
        return Parts(amount, amount - private, dict(zip(names, royalties)))

    def for_reason(self, reason):
        if reason == "sla":
            return Parts(self.public, self.public, self.royalties)
        if reason != "dispute":
            raise LedgerError("Unsupported refund reason")
        return self

    def minus(self, other):
        remaining = self.royalties.copy()
        for owner, n in other.royalties.items():
            remaining[owner] = remaining.get(owner, 0) - n
        return Parts(self.principal - other.principal, self.public - other.public, remaining)

    def earned(self, sign):
        accounts = {payable(owner): sign * n for owner, n in self.royalties.items()}
        accounts[REVENUE] = sign * (self.principal - sum(self.royalties.values()))
        return accounts


def _call_mandate(session, call):
    return session.scalars(select(Mandate).join(AgentRun, AgentRun.mandate_id == Mandate.id)
                           .where(AgentRun.id == call.run_id, Mandate.workspace_id == call.workspace_id)).one()


def _parts(call):
    return Parts(call.charge_micros, call.public_charge_micros, call.royalty_json)


def accrue_call(session, call):
    session.add(call)
    session.flush()
    mandate = _call_mandate(session, call)
    parts = _parts(call)
    if call.platform_micros != parts.principal - sum(parts.royalties.values()):
        raise LedgerError("Platform revenue and royalties do not sum to the charge")
    if parts.royalties:
        owners = set(session.scalars(select(Publisher.id).where(Publisher.workspace_id == call.workspace_id,
                                                                   Publisher.id.in_(parts.royalties))))
        if owners != set(parts.royalties):
            raise LedgerError("Unknown publisher in this workspace")
    changes = parts.earned(-1)
    changes[receivable(mandate.consumer_id)] = parts.principal
    if _txn(session, "call", call.id):
        post(session, call.workspace_id, "call", call.id, changes)
        return
    if mandate.status not in {"ACTIVE", "AUTHORIZED"} or not mandate.authorization_id:
        raise LedgerError("Accrual requires an active authorized mandate")
    if mandate.accrued_uncaptured_micros + mandate.captured_micros + parts.principal > mandate.cap_micros:
        raise LedgerError("Authorization cap exceeded")
    if post(session, call.workspace_id, "call", call.id, changes):
        mandate.accrued_uncaptured_micros += parts.principal
        mandate.last_activity_at = utcnow()


def _allocation_parts(item):
    return Parts(item.principal_micros, item.public_micros, item.royalty_json)


def _open_parts(session, call):
    parts = _parts(call).minus(Parts(call.reversed_micros, call.reversed_public_micros, call.reversed_royalty_json))
    for item in session.scalars(select(CaptureAllocation).where(CaptureAllocation.call_id == call.id)):
        parts = parts.minus(_allocation_parts(item))
    return parts


def reverse_uncaptured(session, call, *, reason="sla"):
    if reason == "sla" and not call.sla_breach:
        raise LedgerError("An SLA reversal requires a measured breach")
    mandate = _call_mandate(session, call)
    if _txn(session, "close_dust", mandate.id):
        # This mandate was closed below the minimum payable cent. Its open
        # principal was already absorbed by the platform at close.
        return 0
    parts = _open_parts(session, call).for_reason(reason)
    if not parts.principal:
        return 0
    changes = parts.earned(+1)
    changes[receivable(mandate.consumer_id)] = -parts.principal
    if post(session, call.workspace_id, f"uncaptured_{reason}", call.id, changes):
        mandate.accrued_uncaptured_micros -= parts.principal
        call.reversed_micros += parts.principal
        call.reversed_public_micros += parts.public
        call.reversed_royalty_json = _combine(call.reversed_royalty_json, parts.royalties)
    return parts.principal


def _combine(previous, delta):
    result = dict(previous)
    for key, value in delta.items():
        result[key] = result.get(key, 0) + value
    return result


def _open_calls(session, mandate):
    return session.scalars(select(Call).join(AgentRun, AgentRun.id == Call.run_id)
                           .where(AgentRun.mandate_id == mandate.id).order_by(Call.created_at, Call.id))


def record_capture(session, capture):
    """Book a confirmed PayPal capture. FIFO allocates principal down to the microdollar."""
    if capture.status not in CAPTURED or not capture.paypal_capture_id:
        raise LedgerError("A confirmed PayPal capture is required")
    session.add(capture)
    session.flush()
    mandate = session.get(Mandate, capture.mandate_id)
    if _txn(session, "capture", capture.id):
        return
    cash = capture.amount_cents * MICROS_PER_CENT
    pending = mandate.accrued_uncaptured_micros
    to_settle = pending if capture.final else cash
    if (cash > mandate.cap_micros - mandate.captured_micros or to_settle > pending
            or (capture.final and capture.amount_cents != round_cents(pending))):
        raise LedgerError("Confirmed capture differs from the authorized accrual")
    remainder = to_settle
    ids = []
    for call in _open_calls(session, mandate):
        if remainder == 0:
            break
        open_parts = _open_parts(session, call)
        if not open_parts.principal:
            continue
        part = open_parts.portion(min(remainder, open_parts.principal))
        session.add(CaptureAllocation(workspace_id=mandate.workspace_id, capture_id=capture.id,
                                      call_id=call.id, principal_micros=part.principal,
                                      public_micros=part.public, royalty_json=part.royalties))
        remainder -= part.principal
        ids.append(call.id)
    if remainder:
        raise LedgerError("Open calls do not cover the capture")
    capture.call_ids_json = ids
    fee = capture.fee_cents * MICROS_PER_CENT
    post(session, mandate.workspace_id, "capture", capture.id, {
        CLEARING: cash - fee, FEES: fee, receivable(mandate.consumer_id): -to_settle, ROUNDING: to_settle - cash,
    })
    mandate.captured_micros += cash
    mandate.accrued_uncaptured_micros -= to_settle
    mandate.last_activity_at = utcnow()


def close_dust(session, mandate):
    """Close a remaining accrual that rounds to zero cents, retaining publisher earnings."""
    pending = mandate.accrued_uncaptured_micros
    if round_cents(pending):
        raise LedgerError("The remaining balance requires a final capture")
    if pending:
        post(session, mandate.workspace_id, "close_dust", mandate.id, {
            ROUNDING: pending, receivable(mandate.consumer_id): -pending,
        })
        mandate.accrued_uncaptured_micros = 0


@dataclass(frozen=True)
class RefundPlan:
    amount_cents: int
    portions: dict[str, Parts]


def plan_refund(session, capture, call_ids, *, reason):
    ids = set(call_ids)
    if not ids or not ids <= set(capture.call_ids_json):
        raise LedgerError("Refund calls must belong to the captured principal")
    portions = {}
    allocations = session.scalars(select(CaptureAllocation).where(CaptureAllocation.capture_id == capture.id,
                                                                  CaptureAllocation.call_id.in_(ids)))
    for item in allocations:
        if reason == "sla" and not session.get(Call, item.call_id).sla_breach:
            raise LedgerError("An SLA refund requires a measured breach")
        refunded = Parts(item.refunded_micros, item.refunded_public_micros, item.refunded_royalty_json)
        part = _allocation_parts(item).minus(refunded).for_reason(reason)
        if part.principal:
            portions[item.call_id] = part
    used = session.scalar(select(func.coalesce(func.sum(Refund.amount_cents), 0))
                          .join(LedgerTxn, (LedgerTxn.ref_id == Refund.id) & (LedgerTxn.ref_type == "refund"))
                          .where(Refund.capture_id == capture.id))
    prior_principal = session.scalar(select(func.coalesce(func.sum(CaptureAllocation.refunded_micros), 0))
                                     .where(CaptureAllocation.capture_id == capture.id))
    additional = sum(part.principal for part in portions.values())
    return RefundPlan(refund_cents(capture.amount_cents, used, prior_principal, additional), portions)


def refund_cents(capture_cents, paid_cents, prior_principal, additional_principal):
    """Round cumulative entitlements once for a capture, including sub-cent claims."""
    if (min(capture_cents, paid_cents, prior_principal, additional_principal) < 0
            or paid_cents > capture_cents):
        raise LedgerError("Invalid cumulative refund")
    if not additional_principal:
        return 0
    target = max(1, round_cents(prior_principal + additional_principal))
    return max(0, min(capture_cents, target) - paid_cents)


def _refund_entries(session, capture, portions):
    changes = defaultdict(int)
    principal = 0
    for call_id, part in portions.items():
        item = session.scalars(select(CaptureAllocation).where(CaptureAllocation.capture_id == capture.id,
                                                               CaptureAllocation.call_id == call_id)).one()
        for account, amount in part.earned(+1).items():
            changes[account] += amount
        principal += part.principal
        item.refunded_micros += part.principal
        item.refunded_public_micros += part.public
        item.refunded_royalty_json = _combine(item.refunded_royalty_json, part.royalties)
    return changes, principal


def apply_refund_credit(session, capture, call_ids, *, reason):
    """Recognize additional microdollars already covered by an earlier PayPal refund."""
    plan = plan_refund(session, capture, call_ids, reason=reason)
    if plan.amount_cents:
        raise LedgerError("The customer is still due a PayPal refund")
    if not plan.portions:
        return None
    confirmations = list(session.scalars(select(Refund.paypal_refund_id).where(
        Refund.capture_id == capture.id, Refund.status == "COMPLETED").order_by(Refund.created_at)))
    if not confirmations or not all(confirmations):
        raise LedgerError("No confirmed PayPal refund covers this credit")
    changes, principal = _refund_entries(session, capture, plan.portions)
    changes[ROUNDING] = -principal
    key = sha256((",".join(sorted(plan.portions))).encode()).hexdigest()
    post(session, capture.workspace_id, "refund_credit", f"{capture.id}:{reason}:{key}", changes)
    return {"amount_cents": 0, "covered_micros": principal, "prior_paypal_refund_ids": confirmations}


def record_refund(session, refund):
    if refund.status != "COMPLETED" or not refund.paypal_refund_id:
        raise LedgerError("A completed PayPal refund is required")
    session.add(refund)
    session.flush()
    if _txn(session, "refund", refund.id):
        return
    capture = session.get(Capture, refund.capture_id)
    plan = plan_refund(session, capture, refund.call_ids_json, reason=refund.reason)
    if refund.amount_cents != plan.amount_cents or not plan.portions:
        raise LedgerError("Confirmed refund differs from the refundable amount")
    changes, principal = _refund_entries(session, capture, plan.portions)
    cash = refund.amount_cents * MICROS_PER_CENT
    fee_credit = refund.fee_reversal_cents * MICROS_PER_CENT
    changes[CLEARING] = -cash + fee_credit
    changes[FEES] = -fee_credit
    changes[ROUNDING] = cash - principal
    post(session, refund.workspace_id, "refund", refund.id, changes)
    session.get(Mandate, capture.mandate_id).refunded_micros += cash


def available_payable(session, workspace_id, publisher_id):
    reserved = session.scalar(select(func.coalesce(func.sum(PayoutItem.amount_cents), 0))
                              .where(PayoutItem.workspace_id == workspace_id, PayoutItem.publisher_id == publisher_id,
                                     PayoutItem.status.not_in((*PAID, "FAILED", "DENIED", "CANCELED", "RETURNED"))))
    return max(0, -balance(session, workspace_id, payable(publisher_id)) - reserved * MICROS_PER_CENT)


def record_payout(session, item, *, fee_cents=None):
    if item.status not in PAID or not item.paypal_item_id:
        raise LedgerError("A successful PayPal payout item is required")
    session.add(item)
    session.flush()
    fee = item.fee_cents if fee_cents is None else fee_cents
    if type(fee) is not int or fee < 0:
        raise LedgerError("Invalid payout fee")
    cash = item.amount_cents * MICROS_PER_CENT
    if post(session, item.workspace_id, "payout", item.id, {
        payable(item.publisher_id): cash, CLEARING: -cash - fee * MICROS_PER_CENT, FEES: fee * MICROS_PER_CENT,
    }):
        item.fee_cents = fee
        session.flush()
        batch = session.get(PayoutBatch, item.batch_id)
        batch.fee_cents = session.scalar(select(func.coalesce(func.sum(PayoutItem.fee_cents), 0))
                                        .where(PayoutItem.batch_id == item.batch_id, PayoutItem.status.in_(PAID)))


def _totals(session, key, amount, *criteria, refund_mandates=False):
    query = select(key, func.sum(amount))
    if refund_mandates:
        query = query.select_from(Refund).join(Capture, Refund.capture_id == Capture.id)
    return dict(session.execute(query.where(*criteria).group_by(key)).all())


def assert_invariants(session, *, workspace_id=None, expected_clearing_micros=None):
    """Check local accounting. Supply an independently GET-verified total for external reconciliation."""
    session.flush()
    where = lambda model: [model.workspace_id == workspace_id] if workspace_id is not None else []
    net = LedgerEntry.debit_micros - LedgerEntry.credit_micros
    unbalanced = session.execute(select(LedgerEntry.txn_id).where(*where(LedgerEntry)).group_by(LedgerEntry.txn_id)
                                 .having(func.sum(net) != 0).limit(1)).first()
    if unbalanced:
        raise LedgerError("Unbalanced transaction: " + unbalanced[0])
    captured = _totals(session, Capture.mandate_id, Capture.amount_cents, Capture.status.in_(CAPTURED), *where(Capture))
    refunded = _totals(session, Capture.mandate_id, Refund.amount_cents, Refund.status == "COMPLETED",
                       *where(Refund), refund_mandates=True)
    for mandate in session.scalars(select(Mandate).where(*where(Mandate))):
        if (mandate.captured_micros != captured.get(mandate.id, 0) * MICROS_PER_CENT or
                mandate.refunded_micros != refunded.get(mandate.id, 0) * MICROS_PER_CENT or
                not 0 <= mandate.refunded_micros <= mandate.captured_micros <= mandate.cap_micros):
            raise LedgerError(f"Mandate totals differ from settlement records: {mandate.id}")
    cash = _totals(session, Capture.workspace_id, Capture.amount_cents - Capture.fee_cents,
                   Capture.status.in_(CAPTURED), *where(Capture))
    returns = _totals(session, Refund.workspace_id, Refund.amount_cents - Refund.fee_reversal_cents,
                      Refund.status == "COMPLETED", *where(Refund))
    payouts = _totals(session, PayoutItem.workspace_id, PayoutItem.amount_cents + PayoutItem.fee_cents,
                      PayoutItem.status.in_(PAID), *where(PayoutItem))
    clearing = _totals(session, LedgerEntry.workspace_id, net, LedgerEntry.account == CLEARING, *where(LedgerEntry))
    for wid in cash.keys() | returns.keys() | payouts.keys() | clearing.keys():
        expected = (cash.get(wid, 0) - returns.get(wid, 0) - payouts.get(wid, 0)) * MICROS_PER_CENT
        if clearing.get(wid, 0) != expected:
            raise LedgerError("PayPal clearing does not match local settlement records")
    pending = _totals(session, Mandate.consumer_id, Mandate.accrued_uncaptured_micros, *where(Mandate))
    outstanding = _totals(session, LedgerEntry.account, net, LedgerEntry.account.like("consumer:%:receivable"),
                          *where(LedgerEntry))
    expected_receivables = {receivable(consumer): amount for consumer, amount in pending.items() if amount}
    if {account: amount for account, amount in outstanding.items() if amount} != expected_receivables:
        raise LedgerError("Consumer receivable differs from total pending mandate accruals")
    observed = sum(clearing.values())
    if expected_clearing_micros is not None and expected_clearing_micros != observed:
        raise LedgerError("PayPal clearing differs from confirmed PayPal readbacks")
    return observed
