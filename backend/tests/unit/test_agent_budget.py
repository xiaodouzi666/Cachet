from pathlib import Path

import pytest

from backend.app.api.agent import BudgetExhausted, ensure_budget_before_next
from backend.app.db import session_scope
from backend.app.gateway.billing import Rates
from backend.app.models import Consumer, Mandate, Workspace
from backend.app.replay.recordings import load


def test_insufficient_budget_stops_before_resuming_the_turn(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "budget.sqlite"))
    with session_scope() as db:
        workspace = Workspace(kind="judge", cookie_id="budget-unit")
        db.add(workspace)
        db.flush()
        consumer = Consumer(workspace_id=workspace.id, name="test", salt_hex="d1603a20")
        db.add(consumer)
        db.flush()
        mandate = Mandate(workspace_id=workspace.id, consumer_id=consumer.id, status="ACTIVE", cap_micros=10_000)
        db.add(mandate)
        db.flush()
        ws, mid = workspace.id, mandate.id
    turn = load("rate-limit", False)["turns"][0]

    def steps():
        yield {"turn": turn["turn"], "kind": "estimate", "spans": turn["spans"], "output_limit": turn["output_tokens"]}
        pytest.fail("The engine must not generate a turn after the budget check rejects it")

    with pytest.raises(BudgetExhausted) as err:
        list(ensure_budget_before_next(steps(), ws, mid, Rates()))
    assert err.value.summary["code"] == "insufficient_budget"
    assert err.value.summary["turn"] == 1
    assert err.value.summary["available_micros"] == 10_000
    assert err.value.summary["required_micros"] > 10_000
