"""Live checks. These create real sandbox authorizations, captures, refunds and payouts."""

import json
import os
from pathlib import Path
import uuid

import pytest

from backend.app.paypal.client import ROOT, settings
from backend.app.paypal.webhooks import wait_for_deliveries
from scripts.sandbox_smoke import run


@pytest.fixture(scope="module")
def wallet_and_payouts():
    root = ROOT / "logs" / ("verify-sandbox-" + str(uuid.uuid4()))
    result = []
    for scenario in ("wallet", "payout"):
        path = root / (scenario + ".json")
        run(scenario, path)
        result.append(json.loads(path.read_text()))
    return result


@pytest.mark.sandbox
def test_authorizations_refunds_and_payouts(wallet_and_payouts):
    wallet, payouts = wallet_and_payouts
    assert wallet["completed"] and payouts["completed"]


@pytest.mark.sandbox
def test_reconcile_released_authorization_without_captures(wallet_and_payouts, tmp_path, monkeypatch):
    from backend.app.db import session_scope
    from backend.app.models import Consumer, Mandate, Workspace
    from backend.app.paypal.reconciliation import reconcile
    from backend.app.settlement.service import cents, close

    wallet, _ = wallet_and_payouts
    order = wallet["readbacks"]["unused.order"]
    voided = wallet["readbacks"]["unused.void"]
    assert voided["status"] == "VOIDED"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "released.db"))
    with session_scope() as db:
        ws = Workspace(cookie_id=uuid.uuid4().hex)
        db.add(ws)
        db.flush()
        consumer = Consumer(workspace_id=ws.id, name="Sandbox recovery", salt_hex=uuid.uuid4().hex)
        db.add(consumer)
        db.flush()
        # Resume after the real void completed but before its local status was saved.
        m = Mandate(workspace_id=ws.id, consumer_id=consumer.id, status="CLOSING",
                    paypal_order_id=order["id"], authorization_id=voided["id"],
                    cap_micros=cents(order["amount"]) * 10000)
        db.add(m)
        db.flush()
        ws_id, mid = ws.id, m.id
    closed = close(ws_id, mid)
    assert closed["status"] == "VOIDED" and closed["released_micros"] == closed["cap_micros"]
    report = reconcile(ws_id)
    assert report["ok"] is True and report["checked"] == 2 and report["clearing_micros"] == 0


@pytest.mark.sandbox
def test_verified_deliveries_for_actual_payments(wallet_and_payouts):
    evidence = wait_for_deliveries(*wallet_and_payouts, seconds=180)
    assert not evidence["missing"], evidence["missing"]


@pytest.mark.sandbox
def test_five_approved_wallets():
    run("approval", ROOT / "logs" / f"verify-approvals-{uuid.uuid4()}.json")


@pytest.mark.sandbox
def test_direct_sandbox_card_when_configured():
    config = settings()
    if not config.get("SANDBOX_TEST_CARD_NUMBER") and not config.get("SANDBOX_TEST_CARD_EXPIRY"):
        pytest.skip("Optional sandbox card path is unconfigured.")
    run("card", ROOT / "logs" / f"verify-card-{uuid.uuid4()}.json")


@pytest.mark.sandbox
def test_fee_aware_close_and_test_capture(monkeypatch):
    """Resume private real journals instead of creating another authorization for an already verified case."""
    from backend.app.db import configured_url
    from scripts.mandate_check import check_case
    monkeypatch.setenv("DATABASE_URL", configured_url())
    directory = ROOT / "logs" / "fee-aware-acceptance"
    unused = check_case(directory, "unused")
    forgiven = check_case(directory, "partial_then_forgive")
    final = check_case(directory, "final")
    assert unused["authorization_status"] == "VOIDED" and not unused["captures"]
    assert forgiven["authorization_status"] == "VOIDED" and forgiven["forgiven_micros"] > 0
    assert forgiven["judge_capture"]["actual"]["net_cents"] == forgiven["captures"][0]["net_cents"]
    assert final["authorization_status"] == "CAPTURED" and final["captures"][-1]["requested_final"]


@pytest.mark.sandbox
def test_recover_committed_writeoff_before_real_void(monkeypatch):
    from sqlalchemy import select
    from backend.app import ledger
    from backend.app.db import session_scope
    from backend.app.models import Mandate
    from backend.app.paypal.client import PayPalClient
    from backend.app.settlement import service
    from backend.app.settlement.mandate_fsm import transition
    from scripts.sandbox_smoke import locked_journal
    from scripts.mandate_check import replay_segment, workspace

    directory = ROOT / "logs" / "fee-aware-acceptance" / "writeoff-recovery"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(directory / "ledger.sqlite"))
    with locked_journal(directory / "journal.json", "writeoff-release-recovery") as journal:
        ws = workspace()
        with session_scope(write=False) as db:
            m = db.scalars(select(Mandate)).one_or_none()
        if m is None or not m.authorization_id:
            mid = service.open_order(ws, 250_000, card=True)["id"]
            service.authorize(ws, mid)
        else:
            mid = m.id
        with session_scope(write=False) as db:
            m = db.get(Mandate, mid)
        if m.status == "ACTIVE":
            replay_segment(ws, mid, 1)
            with session_scope() as db:
                m = db.get(Mandate, mid)
                transition(m, "CLOSING")
                ledger.forgive_at_close(db, m, service.final_minimum())
        if "before_release" not in journal.data["readbacks"]:
            with PayPalClient.from_env() as pp:
                actual = pp.get_authorization(m.authorization_id)
            with session_scope(write=False) as db:
                amount = ledger.written_off_micros(db, mid)
                assert amount > 0 and db.get(Mandate, mid).accrued_uncaptured_micros == 0
            assert actual["status"] == "CREATED"
            journal.record("before_release", {"provider_status":actual["status"], "writeoff_micros":amount})
        closed = service.close(ws, mid)
        with PayPalClient.from_env() as pp:
            actual = pp.get_authorization(closed["authorization_id"])
        assert actual["status"] == "VOIDED" and closed["status"] == "CLOSED" and not closed["captured_micros"]
        assert closed["forgiven_micros"] == journal.data["readbacks"]["before_release"]["writeoff_micros"]
        assert service.close(ws, mid) == closed
        journal.record("after_recovery", {"provider_status":actual["status"], "authorization_id":actual["id"],
                       "mandate_id":mid, "writeoff_micros":closed["forgiven_micros"], "released_micros":closed["released_micros"]})
        journal.data["completed"] = True
        journal.save()


@pytest.mark.sandbox
def test_watchdog_reconciles_submitted_items_without_webhooks(request, tmp_path, monkeypatch):
    from sqlalchemy import func, select
    from backend.app import ledger
    from backend.app.db import session_scope
    from backend.app.models import LedgerTxn, PayoutBatch, PayoutItem, Publisher, Workspace, new_id
    from backend.app.paypal.client import PayPalClient
    from backend.app.settlement.payout_job import refresh_submitted_payouts
    from backend.app.settlement.service import cents

    journal_path = os.environ.get("CACHET_PAYOUT_READBACK_JOURNAL")
    if journal_path:
        payouts = json.loads(Path(journal_path).read_text())
        assert payouts["completed"] and payouts["scenario"] in ("payout", "all")
    else:
        _, payouts = request.getfixturevalue("wallet_and_payouts")
    provider_id = payouts["readbacks"]["payout.batch"]["id"]
    with PayPalClient.from_env() as pp:
        receipt = pp.get_payout(provider_id)
    header = receipt["batch_header"]
    assert header["batch_status"] == "SUCCESS"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "pending-items.sqlite"))
    with session_scope() as db:
        ws = Workspace(cookie_id=new_id(), kind="judge")
        db.add(ws)
        db.flush()
        # The confirmed header arrived before the original item statuses or any postings.
        batch = PayoutBatch(workspace_id=ws.id, status=header["batch_status"], paypal_batch_id=provider_id,
                            sender_batch_id=header["sender_batch_header"]["sender_batch_id"])
        unsubmitted = PayoutBatch(workspace_id=ws.id, status="CREATED", sender_batch_id=new_id())
        db.add_all([batch, unsubmitted])
        db.flush()
        wid, bid, untouched = ws.id, batch.id, unsubmitted.id
        expected = {}
        for row in receipt["items"]:
            detail = row["payout_item"]
            owner = Publisher(workspace_id=wid, name=detail["sender_item_id"], paypal_receiver_email=detail["receiver"])
            db.add(owner)
            db.flush()
            db.add(PayoutItem(id=detail["sender_item_id"], batch_id=bid, workspace_id=wid, publisher_id=owner.id,
                              amount_cents=cents(detail["amount"]), status="PENDING"))
            expected[row["payout_item_id"]] = (cents(detail["amount"]), cents(row["payout_item_fee"]))
    for _ in range(2):
        refresh_submitted_payouts()
        with session_scope(write=False) as db:
            items = list(db.scalars(select(PayoutItem).where(PayoutItem.batch_id == bid)))
            assert len(items) == 2 and all(item.status == "SUCCESS" for item in items)
            assert {item.paypal_item_id: (item.amount_cents, item.fee_cents) for item in items} == expected
            assert db.get(PayoutBatch, untouched).paypal_batch_id is None
            assert db.scalar(select(func.count()).select_from(PayoutBatch)) == 2
            assert db.scalar(select(func.count()).select_from(LedgerTxn).where(LedgerTxn.ref_type == "payout")) == 2
            assert ledger.assert_invariants(db, workspace_id=wid) == -sum(a + f for a, f in expected.values()) * 10000
