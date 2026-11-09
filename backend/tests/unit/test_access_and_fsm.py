import secrets

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api.access import limit
from backend.app.db import session_scope
from backend.app.main import app
from backend.app.models import Mandate
from backend.app.operations import LimitEvent
from backend.app.settlement.mandate_fsm import TRANSITIONS, transition


def test_mandate_state_transitions():
    for source, allowed in TRANSITIONS.items():
        for target in TRANSITIONS:
            mandate = Mandate(status=source)
            if target in allowed:
                transition(mandate, target)
                assert mandate.status == target
                assert bool(mandate.closed_at) == (not TRANSITIONS[target])
            else:
                with pytest.raises(ValueError):
                    transition(mandate, target)
                assert mandate.status == source


def test_judge_consent_and_gateway_access(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'isolated.sqlite'}")
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("ADMIN_TOKEN", token)
    monkeypatch.setenv("PUBLIC_WEB_URL", "http://127.0.0.1:3000")
    headers = {"X-Admin-Token": token}
    with TestClient(app) as browser:
        assert browser.get("/api/judge/state").status_code == 403
        assert browser.get("/api/judge/state", headers=headers).status_code == 401
        browser.headers.update(headers)
        first = browser.post("/api/judge/workspace", json={"kind": "user"})
        assert first.status_code == 200 and "HttpOnly" in first.headers["set-cookie"]
        user = first.json()
        judge = browser.post("/api/judge/workspace", json={"kind": "judge"}).json()
        assert user["workspace_id"] != judge["workspace_id"]
        assert judge["kind"] == "judge"
        assert (
            browser.post("/api/judge/workspace", json={"kind": "user"}).json() == judge
        )
        assert browser.get("/api/ledger").json() == []
        assert browser.get("/api/captures").json() == []
        assert (
            browser.post(
                "/api/judge/settle", json={"batch_id": user["workspace_id"]}
            ).status_code
            == 404
        )
        with session_scope() as db:
            from backend.app.models import Consumer

            consumer = db.scalar(
                select(Consumer).where(Consumer.workspace_id == judge["workspace_id"])
            )
            # This is an unapproved local mandate. No provider transaction exists.
            mandate = Mandate(
                workspace_id=judge["workspace_id"],
                consumer_id=consumer.id,
                cap_micros=5000000,
            )
            db.add(mandate)
            db.flush()
            identifier = mandate.id
        assert (
            browser.get("/api/mandates/" + identifier).json()["authorization_id"]
            is None
        )
        restored = browser.post(
            "/api/judge/workspace", json={"kind": "user", "switch": True}
        ).json()
        assert restored == user
        assert browser.get("/api/mandates/" + identifier).status_code == 404
        assert (
            browser.post(
                "/api/judge/fault", json={"ttft_delay_ms": 1000, "calls": 1}
            ).status_code
            == 403
        )
        assert (
            browser.post("/api/mandates", json={"cap_usd": "20.01"}).status_code == 422
        )


def test_rate_limit_rejection_is_atomic(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'rate.sqlite'}")
    for _ in range(3):
        with session_scope() as db:
            limit(db, ["workspace", "ip"], 3, 3600)
    with pytest.raises(HTTPException) as error, session_scope() as db:
        limit(db, ["unused", "workspace"], 3, 3600)
    assert (
        error.value.status_code == 429 and int(error.value.headers["Retry-After"]) > 0
    )
    with session_scope(write=False) as db:
        assert db.scalar(select(func.count()).select_from(LimitEvent)) == 6
