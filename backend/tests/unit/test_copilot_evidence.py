from uuid import uuid4

from backend.app.agents.anomaly import volume_signal
from backend.app.agents.copilot_tools import earnings, ledger_activity, mandate_breakdown, overview
from backend.app.db import init_db, session_scope
from backend.app.models import Consumer, Mandate, Workspace


def test_new_workspace_evidence_and_scoped_mandate(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path}/evidence.db")
    with session_scope(engine) as db:
        first, second = [Workspace(cookie_id=uuid4().hex) for _ in range(2)]
        db.add_all([first, second])
        db.flush()
        owner = Consumer(workspace_id=first.id, name="Unit test", salt_hex=uuid4().hex[:8])
        db.add(owner)
        db.flush()
        # The local draft has no remote order, authorization, captures or payment IDs.
        mandate = Mandate(workspace_id=first.id, consumer_id=owner.id, cap_micros=5_000_000)
        db.add(mandate)
        db.flush()
    with session_scope(engine, write=False) as db:
        assert "There are no publishers" in earnings(db, second.id)[0]
        assert "no posted entries" in ledger_activity(db, second.id)[0]
        assert "0 mandate records" in overview(db, second.id)[0]
        assert mandate.id in " ".join(mandate_breakdown(db, first.id, mandate.id))
        assert "No matching mandate" in mandate_breakdown(db, second.id, mandate.id)[0]


def test_spike_requires_historical_variance():
    assert volume_signal(10, [0] * 7) is None
    assert volume_signal(10, [2, 1]) is None
    assert volume_signal(100, [5, 5, 6, 5, 4, 5, 5]) > 3
