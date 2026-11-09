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
