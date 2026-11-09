"""Operator dataset provenance starts empty and requires actual authorized traffic."""

import secrets

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.finance import router
from backend.app.db import session_scope
from backend.app.models import Consumer, Mandate


def test_unfunded_mandate_cannot_be_declared_seed(tmp_path, monkeypatch):
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("ADMIN_TOKEN", token)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "workspace.db"))
    monkeypatch.setenv("PUBLIC_WEB_URL", "http://localhost")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = token
        ws = client.post("/api/judge/workspace", json={"kind": "judge"}).json()[
            "workspace_id"
        ]
        state = client.get("/api/judge/state").json()
        assert state["operator_seeded"] is False
        assert state["operator_seed_run_count"] == 0
        assert state["operator_seed_run_ids"] == []
        with session_scope() as db:
            consumer = db.scalar(select(Consumer).where(Consumer.workspace_id == ws))
            draft = Mandate(
                workspace_id=ws, consumer_id=consumer.id, cap_micros=5_000_000
            )
            db.add(draft)
            db.flush()
            draft_id = draft.id
        path = "/api/operator/seed-mandates/" + draft_id
        assert client.post(path).status_code == 409
        client.headers.pop("X-Admin-Token")
        assert client.post(path).status_code == 403
        client.headers["X-Admin-Token"] = token
        assert client.get("/api/judge/state").json()["operator_seed_run_ids"] == []
