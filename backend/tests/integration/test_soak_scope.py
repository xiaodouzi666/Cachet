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
        limited = secrets.token_urlsafe(32)
        monkeypatch.setenv("BFF_TOKEN", limited)
        assert client.get(route, headers={"X-Gateway-Token": limited, "X-Cachet-Soak-Token": operator}).status_code == 403
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


def test_recording_gate_uses_payable_funding_not_gross_earnings():
    # These values describe a gate calculation, not a provider receipt.
    state = {"threshold_captures":2, "fault_turns":6, "fault_refund_ids":[True],
             "publisher_payable_micros":{"starlette":300_000,"fastapi":260_000},
             "publisher_available_micros":{"starlette":250_000,"fastapi":240_000}}
    assert not soak.recording_targets_met(state)
    state["publisher_available_micros"]["fastapi"] = 250_000
    assert soak.recording_targets_met(state)
    state["fault_turns"] = 1
    assert not soak.recording_targets_met(state)


def test_operator_selects_target_credential_and_signals_pending_c2(monkeypatch):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from soak_mandate import admin_for_origin, outcome, PENDING_TARGETS
    config = {"ADMIN_TOKEN":"local", "PRODUCTION_ADMIN_TOKEN":"deployed"}
    assert admin_for_origin(config, "http://127.0.0.1:8000") == "local"
    assert admin_for_origin(config, "http://[::1]:8000") == "local"
    assert admin_for_origin(config, "https://api.example.invalid") == "deployed"
    state = {"threshold_captures":2, "fault_turns":5, "fault_refund_ids":[True],
             "publisher_available_micros":{"starlette":300_000, "fastapi":260_000},
             "billable_micros":4_200_000, "target_micros":4_200_000, "until":100}
    assert outcome(state, 99) == ("spending_target_reached_with_c2_pending", PENDING_TARGETS)
    state["billable_micros"] = 4_100_000
    assert outcome(state, 101) == ("time_window_ended_with_c2_pending", PENDING_TARGETS)
    assert outcome(state, 99) == ("running", 0)
    state["fault_turns"] = 6
    assert outcome(state, 101) == ("recording_targets_met", 0)
