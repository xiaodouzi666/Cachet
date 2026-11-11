"""HTTP admission fallback tests use an isolated database and no inference or provider."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api import agent
from backend.app.config import calibration
from backend.app.db import session_scope
from backend.app.models import AgentRun, Base, Consumer, Context, Mandate, Publisher, Workspace
from backend.app.operations import LimitEvent, RunEvent


@pytest.fixture
def admission(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'admission.sqlite'}")
    with session_scope() as db:
        workspace = Workspace(kind="judge", cookie_id="admission-unit")
        db.add(workspace)
        db.flush()
        consumer = Consumer(workspace_id=workspace.id, name="Unit buyer", salt_hex="ab" * 16)
        publisher = Publisher(workspace_id=workspace.id, name="Unit publisher", paypal_receiver_email="")
        db.add_all([consumer, publisher])
        db.flush()
        # This marker exists only in the throwaway database; no provider authorization is created.
        mandate = Mandate(workspace_id=workspace.id, consumer_id=consumer.id,
                          authorization_id="unit-only-no-provider", status="ACTIVE", cap_micros=1_000_000)
        db.add(mandate)
        contexts = {}
        for slug in ("starlette", "fastapi"):
            context = Context(workspace_id=workspace.id, publisher_id=publisher.id,
                              slug=slug, title=slug, source_url="https://example.invalid/unit",
                              license="MIT", version="unit", tokenizer="unit", token_len=1000,
                              token_hash="unit", text_path="unit", status="hot")
            db.add(context)
            db.flush()
            contexts[slug] = context.id
        db.flush()
        ws, mid = workspace.id, mandate.id
    application = FastAPI()
    application.include_router(agent.router)
    application.dependency_overrides[agent.gateway] = lambda: None
    application.dependency_overrides[agent.current_workspace] = lambda: ws
    started = []
    monkeypatch.setattr(agent, "execute", lambda *args: started.append(args))
    monkeypatch.setattr(agent, "health", lambda: {"mode": "live"})
    monkeypatch.setattr(agent, "calibration", lambda mode: calibration("replay"))
    with TestClient(application) as client:
        yield SimpleNamespace(client=client, ws=ws, mid=mid, contexts=contexts, started=started)


def database_rows():
    with session_scope(write=False) as db:
        return {table.name: list(db.execute(select(table))) for table in Base.metadata.sorted_tables}


@pytest.mark.parametrize("task_id", ["custom", "route-class", "rate-limit"])
def test_custom_text_is_rejected_before_health_or_any_admission_side_effect(admission, monkeypatch, task_id):
    def unexpected():
        pytest.fail("Rejected free-text tasks must not probe inference or begin admission")

    monkeypatch.setattr(agent, "health", unexpected)
    before = database_rows()
    for _ in range(4):  # More than the workspace quota: rejected requests must not consume it.
        response = admission.client.post("/api/agent/runs", json={
            "mandate_id": admission.mid, "context_id": admission.contexts["starlette"],
            "task_id": task_id, "task_text": "Create a custom Python module.",
        })
        assert response.status_code == 422
        assert response.json() == {"detail": "Custom tasks are unavailable. Select a built-in task."}
    assert admission.started == []
    assert database_rows() == before


@pytest.mark.parametrize("task_id,slug", [("route-class", "starlette"), ("rate-limit", "fastapi")])
def test_builtin_task_is_still_admitted_after_custom_rejections(admission, task_id, slug):
    body = {"mandate_id": admission.mid, "context_id": admission.contexts[slug], "task_id": task_id}
    for _ in range(4):
        assert admission.client.post("/api/agent/runs", json={
            **body, "task_text": "Create a custom Python module.",
        }).status_code == 422
    response = admission.client.post("/api/agent/runs", json={**body, "task_text": None})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "queued"
    assert len(admission.started) == 1
    with session_scope(write=False) as db:
        run = db.get(AgentRun, result["run_id"])
        assert run.task_id == task_id and run.task_text == agent.TASKS[task_id][1]
        assert db.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert db.scalar(select(func.count()).select_from(LimitEvent)) == 2
        assert [event.event_type for event in db.scalars(select(RunEvent))] == ["queued"]


def test_historical_custom_events_remain_readable_without_changing_raw_output(admission):
    raw_answer = 'Triple backticks in an old answer are preserved: ` ` `'
    with session_scope() as db:
        run = AgentRun(workspace_id=admission.ws, mandate_id=admission.mid,
                       task_id="custom", task_text="Historical custom task", status="completed")
        db.add(run)
        db.flush()
        db.add(RunEvent(run_id=run.id, event_type="turn", payload={"output_text": raw_answer}))
        db.add(RunEvent(run_id=run.id, event_type="done", payload={"status": "completed"}))
        identifier = run.id
    before = database_rows()
    response = admission.client.get(f"/api/agent/runs/{identifier}/events")
    assert response.status_code == 200
    assert "event: turn" in response.text and raw_answer in response.text
    assert "event: done" in response.text
    assert database_rows() == before
    assert admission.started == []
