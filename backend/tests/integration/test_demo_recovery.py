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


def test_completed_journal_recovers_export_before_connecting(tmp_path, monkeypatch):
    import json
    from argparse import Namespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from e2e_demo import execute
    from sandbox_smoke import Journal, now

    path = tmp_path / "operator.json"
    journal = Journal(path, "operator-demo")
    stamp = now()
    journal.data.update(workspace_id="local-export-check", health={"mode": "live"}, warm={}, sessions=[],
                        acceptance_suite={"started": stamp, "completed": stamp, "initial": 0, "repeats": 3})
    journal.save()
    original = path.read_bytes()
    report = tmp_path / "operator-evidence.json"
    # This invalid origin would fail immediately on any path that can contact the backend.
    args = Namespace(state=path, api="invalid-origin", payment="wallet", require_mode="live", repeats=3, new_suite=True)
    # Resume a crash between the suite's durable completion and its atomic report rename.
    for stale in (None, "{}", '{"acceptance_suite":', json.dumps({"workspace_id": "another workspace",
                                                                  "acceptance_suite": journal.data["acceptance_suite"]})):
        if stale is not None:
            report.write_text(stale)
        execute(args)
        evidence = json.loads(report.read_bytes())
        assert evidence["acceptance_suite"] == journal.data["acceptance_suite"]
        assert evidence["workspace_id"] == journal.data["workspace_id"]
        assert path.read_bytes() == original
    args.new_suite = False
    execute(args)
    assert path.read_bytes() == original
    args.repeats = 2
    with pytest.raises(RuntimeError, match="different repeat count"):
        execute(args)
    args.new_suite = True
    with pytest.raises(ValueError, match="HTTPS backend origin"):
        execute(args)
    assert path.read_bytes() == original


def test_incomplete_read_retries_but_completed_invalid_run_is_retired(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from e2e_demo import Demo
    from sandbox_smoke import Journal
    from backend.app.api.agent import emit

    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("ADMIN_TOKEN", token)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "short-run.db"))
    monkeypatch.setenv("PUBLIC_WEB_URL", "http://localhost")
    app = FastAPI()
    app.include_router(finance_router)
    app.include_router(agent_router)
    with TestClient(app, base_url="http://localhost", headers={"X-Admin-Token": token}) as api:
        ws = api.post("/api/judge/workspace", json={"kind": "judge"}).json()["workspace_id"]
        with session_scope() as db:
            consumer = db.scalar(select(Consumer).where(Consumer.workspace_id == ws))
            m = Mandate(workspace_id=ws, consumer_id=consumer.id, cap_micros=50000)
            db.add(m)
            db.flush()
            run = AgentRun(workspace_id=ws, mandate_id=m.id, task_id="rate-limit", task_text="local lifecycle check",
                           mode="replay", status="completed")
            db.add(run)
            db.flush()
            mid, rid = m.id, run.id
            emit(db, rid, "queued", {"mode": "replay"})
            emit(db, rid, "decision", {"decision": "buy"})
        path = tmp_path / "short.json"
        journal = Journal(path, "operator-demo")
        journal.data.update(workspace_cookie=api.cookies["cachet_workspace"], workspace_id=ws,
                            sessions=[{"purpose":"acceptance", "accepted":"prior", "finished":"prior"},
                                      {"purpose":"acceptance", "mandate_id":mid, "run_id":rid}],
                            acceptance_suite={"initial":0, "repeats":3})
        journal.save()
        demo = Demo(journal, api, None, {}, "wallet", mode="replay")
        session = demo.sessions[-1]
        # End of stream by itself must never retire the mandate or reset the consecutive count.
        with pytest.raises(RuntimeError, match="No completed event"):
            demo.session_turns(session)
        assert "failure" not in session and journal.data["acceptance_suite"]["initial"] == 0
        with session_scope() as db:
            emit(db, rid, "done", {"run_id": rid, "status": "completed"})
        requests = []
        api.event_hooks["request"].append(lambda req: requests.append((req.method, req.url.path)))
        # No provider is contacted: the real mandate GET rejects this local-only, unauthorized fixture.
        with pytest.raises(RuntimeError, match="order or authorization differs"):
            demo.session_turns(session)
        assert requests == [("GET", f"/api/agent/runs/{rid}/events"), ("GET", f"/api/mandates/{mid}")]
        saved = Journal(path, "operator-demo")
        assert saved.data["acceptance_suite"]["initial"] == 1
        assert "ten distinct" in saved.data["sessions"][-1]["failure"]["reason"]
        assert saved.data["sessions"][-1]["turns"] == []
        requests.clear()
        with pytest.raises(RuntimeError, match="order or authorization differs"):
            Demo(saved, api, None, {}, "wallet", mode="replay").connect("http://localhost")
        assert requests == [("GET", "/api/judge/state"), ("GET", f"/api/mandates/{mid}")]
        with session_scope(write=False) as db:
            assert db.scalar(select(func.count()).select_from(Mandate)) == 1
            assert db.scalar(select(func.count()).select_from(AgentRun)) == 1


def test_fault_free_recorded_turns_are_a_terminal_acceptance_failure(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from backend.app.replay.recordings import load
    from e2e_demo import FailedRun, check_injected_faults

    turns = load("rate-limit", True)["turns"]
    assert len(turns) == 10 and not any(t["fault_injected_ms"] for t in turns)
    with pytest.raises(FailedRun, match="three observed delays") as exc:
        check_injected_faults(turns)
    assert exc.value.rows == turns
