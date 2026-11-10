"""Verify operator authorization and scheduling boundaries without creating provider transactions."""

import secrets
from datetime import timedelta

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app import soak
from backend.app.api.agent import router as agent_router
from backend.app.api.finance import router as finance_router
from backend.app.db import session_scope
from backend.app.models import Consumer, Mandate, Workspace, utcnow
from backend.app.operations import Control


def test_operator_soak_needs_independent_secret_and_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'scope.db'}")
    general, operator = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    monkeypatch.setenv("ADMIN_TOKEN", general)
    monkeypatch.setenv("CACHET_SOAK_TOKEN", operator)
    app = FastAPI()
    app.include_router(agent_router)
    app.include_router(finance_router)
    with TestClient(app) as client:
        route = "/api/operator/soak-registration"
        admin = {"X-Admin-Token":general}
        full = {**admin, "X-Cachet-Soak-Token":operator}
        assert client.get(route, headers=admin).status_code == 403
        assert client.get(route, headers={"X-Cachet-Soak-Token":operator}).status_code == 403
        assert client.get(route, headers={**admin, "X-Cachet-Soak-Token":general}).status_code == 403
        assert client.get(route, headers=full).json() == {"workspace_id":None, "mandate_id":None}
        assert client.post("/api/operator/soak/reserve", json={"mandate_id":"unbound", "context_id":"unbound"}, headers=full).status_code == 401
        client.post("/api/judge/workspace", json={"kind":"judge"}, headers=admin).raise_for_status()
        assert client.post("/api/operator/soak/unbound/runs", json={"sequence":1}, headers=full).status_code == 403
        assert client.post("/api/operator/soak/unbound/runs", json={"sequence":1,"task_text":"arbitrary"}, headers=full).status_code == 422
        monkeypatch.setenv("CACHET_SOAK_TOKEN", general)
        assert client.get(route, headers=full).status_code == 503


def test_scheduler_refuses_stale_steps_budget_and_elapsed_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'schedule.db'}")
    with session_scope() as db:
        ws = Workspace(kind="judge", cookie_id=secrets.token_hex())
        db.add(ws); db.flush()
        person = Consumer(workspace_id=ws.id, name="Unfunded scheduling test", salt_hex=secrets.token_hex())
        db.add(person); db.flush()
        m = Mandate(workspace_id=ws.id, consumer_id=person.id, cap_micros=5_000_000, status="ACTIVE")
        db.add(m); db.flush()
        marker = Control(key=soak.REGISTRATION, data={"workspace_id":ws.id, "mandate_id":m.id,
            "context_id":None, "started":utcnow().timestamp(), "until":(utcnow()+timedelta(hours=3)).timestamp(),
            "target_micros":4_200_000, "sequence":0, "last_run_id":None, "extensions":0})
        db.add(marker); db.flush()
        with pytest.raises(HTTPException, match="outside"):
            soak.registered(db, "another workspace", m.id)
        with pytest.raises(HTTPException, match="stale"):
            soak.admit(db, ws.id, m.id, 2)
        start, prior = soak.admit(db, ws.id, m.id, 1)
        assert start is marker and prior is None
        marker.data = {**marker.data, "until":utcnow().timestamp() - 1}
        with pytest.raises(HTTPException, match="target"):
            soak.admit(db, ws.id, m.id, 1)
        extended = soak.extend(db, ws.id, m.id, 1)
        assert extended["target_micros"] < m.cap_micros and extended["extensions"] == 1
        assert soak.extend(db, ws.id, m.id, 1)["until"] == extended["until"]
