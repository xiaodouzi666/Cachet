from pathlib import Path
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app import ledger
from backend.app.api.agent import router as agent_router
from backend.app.api.finance import router as finance_router
from backend.app.db import session_scope
from backend.app.models import AgentRun, Consumer, Mandate
from backend.app.settlement.watchdog import recovery


def test_recover_zero_turn_run_without_starting_another(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from e2e_demo import Demo
    from sandbox_smoke import Journal

    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("ADMIN_TOKEN", token)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "runs.db"))
    monkeypatch.setenv("PUBLIC_WEB_URL", "http://localhost")
    app = FastAPI()
    app.include_router(finance_router)
    app.include_router(agent_router)
    path = tmp_path / "operator.json"
    with TestClient(app, base_url="http://localhost", headers={"X-Admin-Token": token}) as api:
        ws = api.post("/api/judge/workspace", json={"kind": "judge"}).json()["workspace_id"]
        with session_scope() as db:
            consumer = db.scalar(select(Consumer).where(Consumer.workspace_id == ws))
            mandate = Mandate(workspace_id=ws, consumer_id=consumer.id, cap_micros=5_000_000)
            db.add(mandate)
            db.flush()
            run = AgentRun(workspace_id=ws, mandate_id=mandate.id, task_id="rate-limit", task_text="Test recovery")
            db.add(run)
            db.flush()
            mid, rid = mandate.id, run.id
        recovery()
        journal = Journal(path, "operator-demo")
        journal.data.update(
            workspace_cookie=api.cookies["cachet_workspace"], workspace_id=ws,
            sessions=[{"purpose": "acceptance", "mandate_id": mid}],
        )
        journal.save()
        requests = []
        api.event_hooks["request"].append(lambda r: requests.append((r.method, r.url.path)))
        for attempt in range(2):
            demo = Demo(Journal(path, "operator-demo"), api, None, {}, "wallet")
            # This local mandate never had a sandbox authorization. Recovery must stop at
            # that fact, including on the second invocation, without issuing a new order.
            with pytest.raises(RuntimeError, match="order or authorization differs"):
                demo.connect("http://localhost")
            saved = Journal(path, "operator-demo").data["sessions"]
            assert len(saved) == 1
            assert saved[0]["run_id"] == rid and saved[0]["turns"] == []
            assert "restarted" in saved[0]["failure"]["reason"]
            assert saved[0]["accepted_at"] and "finished" not in saved[0]
            expected = [("GET", "/api/judge/state")]
            if attempt == 0:
                expected.append(("GET", f"/api/agent/runs/{rid}/events"))
            assert requests == expected + [("GET", f"/api/mandates/{mid}")]
            requests.clear()
        audit = api.post("/api/judge/audit", json={"mandate_id": mid}).json()
        assert audit == {"calls_reviewed": 0, "reversed_micros": 0, "refunds": []}
        assert api.get("/api/captures").json() == api.get("/api/refunds").json() == []
        with session_scope(write=False) as db:
            assert db.scalar(select(func.count()).select_from(Mandate)) == 1
            assert db.scalar(select(func.count()).select_from(AgentRun)) == 1
            assert ledger.assert_invariants(db, workspace_id=ws, expected_clearing_micros=0) == 0


def test_payout_selection_skips_retired_failure(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from e2e_demo import pending_acceptance
    from sandbox_smoke import Journal, now

    path = tmp_path / "selection.json"
    journal = Journal(path, "operator-demo")
    failed = {"purpose": "acceptance", "failure": {"reason": "interrupted"}}
    journal.data["sessions"] = [failed]
    journal.save()
    assert pending_acceptance(Journal(path, "operator-demo").data["sessions"]) == failed
    failed["finished"] = now()
    journal.save()
    assert pending_acceptance(Journal(path, "operator-demo").data["sessions"]) is None
