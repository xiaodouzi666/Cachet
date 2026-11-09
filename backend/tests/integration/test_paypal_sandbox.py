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
