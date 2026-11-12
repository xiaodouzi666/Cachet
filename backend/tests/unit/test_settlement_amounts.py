"""Ordinary-workspace settlement with synthetic receipts and a temporary SQLite ledger."""
import pytest
from sqlalchemy import func, select

from backend.app import ledger
from backend.app.api import finance
from backend.app.agents import copilot_tools
from backend.app.db import session_scope
from backend.app.models import (
    AgentRun, Call, Capture, Consumer, Mandate, PayoutBatch, PayoutItem, Publisher,
    Refund, Workspace, LedgerEntry, LedgerTxn,
)
from backend.app.settlement.payout_job import allocation


@pytest.fixture
def funded_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/settlement.db")
    monkeypatch.setenv("PAYOUT_MIN_USD", "0.25")
    with session_scope() as db:
        ws = Workspace(cookie_id="unit-workspace", kind="user")
        db.add(ws)
        db.flush()
        buyer = Consumer(workspace_id=ws.id, name="Buyer", salt_hex="1234abcd")
        publisher = Publisher(workspace_id=ws.id, name="Publisher", paypal_receiver_email="")
        db.add_all([buyer, publisher])
        db.flush()
        mandate = Mandate(workspace_id=ws.id, consumer_id=buyer.id, status="ACTIVE",
                          authorization_id="unit-auth", cap_micros=20_000_000)
        db.add(mandate)
        db.flush()
        run = AgentRun(workspace_id=ws.id, mandate_id=mandate.id, task_id="unit", task_text="unit")
        db.add(run)
    return run, publisher


def capture_turn(db, run, publisher, turn, royalty_micros):
    principal = 2_000_000
    call = Call(workspace_id=run.workspace_id, run_id=run.id, turn=turn,
                prompt_tokens=2, cached_tokens=1, spans_json=[], hits_json={}, output_tokens=1,
                input_hash="unit", output_text="unit", ttft_ms=1, total_ms=2, mode="replay",
                charge_micros=principal, public_charge_micros=principal,
                platform_micros=principal - royalty_micros, royalty_json={publisher.id: royalty_micros})
    ledger.accrue_call(db, call)
    capture = Capture(workspace_id=run.workspace_id, mandate_id=run.mandate_id,
                      paypal_capture_id=f"unit-capture-{turn}", status="COMPLETED",
                      amount_cents=200, fee_cents=41, final=False)
    ledger.record_capture(db, capture)
    return capture


def amounts(row):
    return row["amount_cents"], row["fee_cents"], row["net_cents"]


def test_ordinary_payout_threshold_and_subcent_carry(funded_workspace):
    run, publisher = funded_workspace
    ws = run.workspace_id
    with session_scope() as db:
        capture_turn(db, run, publisher, 1, 249_999)
        assert allocation(db, ws)[2]["items"] == []
        capture_turn(db, run, publisher, 2, 1)
        assert allocation(db, ws)[2]["amount_cents"] == 25

        capture_turn(db, run, publisher, 3, 7_345)
        batch, rows, preview = allocation(db, ws)
        assert batch is None
        assert [(payee.id, cents) for payee, cents in rows] == [(publisher.id, 25)]
        batch = PayoutBatch(workspace_id=ws, sender_batch_id="unit-batch", status="PENDING")
        db.add(batch)
        db.flush()
        item = PayoutItem(workspace_id=ws, batch_id=batch.id, publisher_id=publisher.id,
                          amount_cents=preview["amount_cents"], status="PENDING")
        db.add(item)

    pending, = finance.payouts(ws)
    assert amounts(pending) == (25, None, None)
    assert amounts(pending["items"][0]) == (25, None, None)

    with session_scope() as db:
        batch = db.get(PayoutBatch, batch.id)
        item = db.get(PayoutItem, item.id)
        batch.status = item.status = "SUCCESS"
        item.paypal_item_id = "unit-payout"
        ledger.record_payout(db, item, fee_cents=3)
        assert ledger.available_payable(db, ws, publisher.id) == 7_345
        assert allocation(db, ws)[2]["items"] == []

        capture_turn(db, run, publisher, 4, 242_654)
        assert ledger.available_payable(db, ws, publisher.id) == 249_999
        assert allocation(db, ws)[2]["items"] == []
        capture_turn(db, run, publisher, 5, 1)
        assert allocation(db, ws)[2]["items"] == [
            {"publisher_id": publisher.id, "name": publisher.name, "amount_cents": 25}
        ]

    paid, = finance.payouts(ws)
    assert amounts(paid) == (25, 3, 28)
    assert amounts(paid["items"][0]) == (25, 3, 28)


def test_capture_and_refund_columns_use_booked_fees(funded_workspace):
    run, publisher = funded_workspace
    ws = run.workspace_id
    with session_scope() as db:
        capture = capture_turn(db, run, publisher, 1, 30_000)
        refund = Refund(workspace_id=ws, capture_id=capture.id, paypal_refund_id="unit-refund",
                        amount_cents=200, fee_reversal_cents=8, status="COMPLETED",
                        reason="dispute", call_ids_json=capture.call_ids_json)
        ledger.record_refund(db, refund)

    captured, = finance.captures(ws)
    refunded, = finance.refunds(ws)
    assert amounts(captured) == (200, 41, 159)
    assert amounts(refunded) == (200, 8, 192)


def capture_two_calls(db, run, publisher):
    calls = []
    for turn in (1, 2):
        call = Call(
            workspace_id=run.workspace_id, run_id=run.id, turn=turn,
            prompt_tokens=2, cached_tokens=1, spans_json=[], hits_json={}, output_tokens=1,
            input_hash="unit", output_text="unit", ttft_ms=1, total_ms=2, mode="replay",
            charge_micros=1_000_000, public_charge_micros=1_000_000,
            platform_micros=985_000, royalty_json={publisher.id: 15_000},
        )
        ledger.accrue_call(db, call)
        calls.append(call.id)
    capture = Capture(
        workspace_id=run.workspace_id, mandate_id=run.mandate_id,
        paypal_capture_id="unit-capture-split", status="COMPLETED",
        amount_cents=200, fee_cents=41, final=False,
    )
    ledger.record_capture(db, capture)
    return capture, calls


def assert_capture_readers(run, capture, expected):
    with session_scope(write=False) as db:
        before = (
            db.get(Capture, capture.id).status,
            db.get(Mandate, run.mandate_id).refunded_micros,
            *(db.scalar(select(func.count()).select_from(model))
              for model in (LedgerTxn, LedgerEntry, Refund)),
        )
        texts = [
            *copilot_tools.mandate_breakdown(db, run.workspace_id, run.mandate_id),
            *copilot_tools.payment_activity(db, run.workspace_id),
        ]
        capture_lines = [line for line in texts if line.startswith(f"Capture {capture.paypal_capture_id}")]
        assert len(capture_lines) == 2
        assert all(f"is {expected}" in line for line in capture_lines)
    for _ in range(2):
        captured, = finance.captures(run.workspace_id)
        assert captured["status"] == expected
        assert amounts(captured) == (200, 41, 159)
    with session_scope(write=False) as db:
        after = (
            db.get(Capture, capture.id).status,
            db.get(Mandate, run.mandate_id).refunded_micros,
            *(db.scalar(select(func.count()).select_from(model))
              for model in (LedgerTxn, LedgerEntry, Refund)),
        )
        assert before == after


def test_capture_readers_project_legacy_refunds_without_mutation(funded_workspace):
    run, publisher = funded_workspace
    with session_scope() as db:
        capture, calls = capture_two_calls(db, run, publisher)
        pending = Refund(
            workspace_id=run.workspace_id, capture_id=capture.id,
            paypal_refund_id="unit-pending-refund", amount_cents=100,
            status="PENDING", reason="dispute", call_ids_json=[calls[1]],
        )
        db.add(pending)
    # A provider refund ID alone does not confirm the pending refund.
    assert_capture_readers(run, capture, "COMPLETED")

    with session_scope() as db:
        first = Refund(
            workspace_id=run.workspace_id, capture_id=capture.id,
            paypal_refund_id="unit-first-refund", amount_cents=100,
            status="COMPLETED", reason="dispute", call_ids_json=[calls[0]],
        )
        ledger.record_refund(db, first)
    assert_capture_readers(run, capture, "PARTIALLY_REFUNDED")
    with session_scope() as db:
        first = db.get(Refund, first.id)
        ledger.record_refund(db, first)
        assert db.get(Mandate, run.mandate_id).refunded_micros == 1_000_000
        assert db.scalar(select(func.count()).select_from(LedgerTxn).where(
            LedgerTxn.ref_type == "refund"
        )) == 1
        # A historical capture stays COMPLETED; every reader must handle that row.
        assert db.get(Capture, capture.id).status == "COMPLETED"
        second = db.get(Refund, pending.id)
        second.status = "COMPLETED"
        ledger.record_refund(db, second)
    assert_capture_readers(run, capture, "REFUNDED")


@pytest.mark.parametrize("status", ["PENDING", "DECLINED"])
def test_capture_readers_preserve_unsuccessful_provider_status(funded_workspace, status):
    run, _ = funded_workspace
    with session_scope() as db:
        capture = Capture(
            workspace_id=run.workspace_id, mandate_id=run.mandate_id,
            paypal_capture_id="unit-unposted-capture", status=status,
            amount_cents=200, fee_cents=41, final=False,
        )
        db.add(capture)
    assert_capture_readers(run, capture, status)


@pytest.mark.parametrize("status", ["PARTIALLY_REFUNDED", "REFUNDED"])
def test_capture_readers_do_not_downgrade_provider_status(funded_workspace, status):
    run, publisher = funded_workspace
    with session_scope() as db:
        capture = capture_turn(db, run, publisher, 1, 30_000)
        capture.status = status
    # Local refund history can lag a confirmed provider capture state.
    assert_capture_readers(run, capture, status)


@pytest.mark.parametrize("provider_id", [None, ""])
def test_capture_projection_ignores_unconfirmed_refund_identifier(funded_workspace, provider_id):
    run, publisher = funded_workspace
    with session_scope() as db:
        capture = capture_turn(db, run, publisher, 1, 30_000)
        refund = Refund(
            workspace_id=run.workspace_id, capture_id=capture.id,
            paypal_refund_id="unit-confirmed-refund", amount_cents=200,
            status="COMPLETED", reason="dispute", call_ids_json=capture.call_ids_json,
        )
        ledger.record_refund(db, refund)
        refund.paypal_refund_id = provider_id
    # Synthetic incomplete receipt metadata is not enough to advance the displayed status.
    assert_capture_readers(run, capture, "COMPLETED")
