"""Live checks. These create real sandbox authorizations, captures, refunds and payouts."""

import json
import time
import uuid

import pytest

from backend.app.paypal.client import ROOT, settings
from backend.app.paypal.webhooks import delivery_evidence
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
    deadline = time.monotonic() + 180
    while True:
        evidence = delivery_evidence(*wallet_and_payouts)
        if not evidence["missing"] or time.monotonic() >= deadline:
            break
        time.sleep(3)
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
