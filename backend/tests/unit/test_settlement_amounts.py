"""Ordinary-workspace settlement with synthetic receipts and a temporary SQLite ledger."""
import pytest

from backend.app import ledger
from backend.app.api import finance
from backend.app.db import session_scope
from backend.app.models import (
    AgentRun, Call, Capture, Consumer, Mandate, PayoutBatch, PayoutItem, Publisher,
    Refund, Workspace,
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
