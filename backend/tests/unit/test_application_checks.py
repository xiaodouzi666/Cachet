"""Synthetic receipts exercise trust and persistence; no Docker, GPU or PayPal calls."""

from copy import deepcopy
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from backend.app import application_checks as checks
from backend.app.api import application_checks as api, finance
from backend.app.api.access import digest
from backend.app.db import session_scope
from backend.app.models import AgentRun, Call, Consumer, Mandate, Workspace
from backend.app.operations import Control, RunEvent

ANSWER = "```python\nfrom starlette.applications import Starlette\napp = Starlette()\n```"
HTTP = {
    "status_codes": [200, 404, 200], "success_body": {"pong": True},
    "x_route_class": "custom", "subclass_probe": True,
}
ADMIN = {"X-Admin-Token": "unit-admin-token"}
GATEWAY = {"X-Gateway-Token": "unit-bff-token"}
BASE = "/api/admin/application-checks"


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/checks.sqlite3")
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN["X-Admin-Token"])
    monkeypatch.setenv("BFF_TOKEN", GATEWAY["X-Gateway-Token"])
    now = [2_000_000_000.0]
    monkeypatch.setattr(checks, "clock", lambda: now[0])
    with session_scope() as db:
        ws = Workspace(cookie_id=digest("unit-browser-cookie"), kind="judge")
        other = Workspace(cookie_id=digest("unit-other-cookie"), kind="user")
        db.add_all([ws, other])
        db.flush()
        buyer = Consumer(workspace_id=ws.id, name="Unit buyer", salt_hex="1234abcd")
        db.add(buyer)
        db.flush()
        mandate = Mandate(workspace_id=ws.id, consumer_id=buyer.id, cap_micros=5_000_000)
        db.add(mandate)
        db.flush()
        run = AgentRun(
            workspace_id=ws.id, mandate_id=mandate.id, task_id="route-class",
            task_text="Unit built-in answer", status="completed", mode="live", quote_json={},
        )
        db.add(run)
        db.flush()
        for turn in range(1, 11):
            db.add(Call(
                workspace_id=ws.id, run_id=run.id, turn=turn,
                prompt_tokens=1, cached_tokens=0, spans_json=[], hits_json={},
                output_tokens=1, input_hash="unit-input", output_text=ANSWER,
                ttft_ms=1, total_ms=2, charge_micros=0, public_charge_micros=0,
                royalty_json={}, platform_micros=0, mode="live",
            ))
        db.add(RunEvent(run_id=run.id, event_type="done", payload={
            "run_id": run.id, "status": "completed",
            "application_check": {"status": "unverified", "reason": "Original live event"},
            "verified_application_turn": None,
        }))
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        client.cookies.set("cachet_workspace", "unit-browser-cookie")
        yield client, run.id, ws.id, now


def enqueue_and_claim(scenario):
    client, run_id, _, _ = scenario
    queued = client.post(f"{BASE}/enqueue/{run_id}", headers=ADMIN)
    assert queued.status_code == 200
    assert queued.json()["application_check"]["status"] == "checking"
    result = client.post(f"{BASE}/claim", headers=ADMIN, json={"worker_id": "unit-worker"})
    assert result.status_code == 200
    return result.json()["job"]


def receipt(job, *, status="passed"):
    from backend.app.agents.tasks import app_code
    result = {
        "answer_sha256": job["answer_sha256"],
        "source_sha256": checks.digest(app_code(job["answer"], allow_prose=True)),
        "requested_format_met": not job["answer"].startswith("Extra prose"),
        "status": status,
    }
    if status == "passed":
        result["http"] = deepcopy(HTTP)
    else:
        result["reason"] = "Synthetic verifier failure for the receipt-state unit test"
    return result


def payload(job, **result):
    return {
        **{key: job[key] for key in ("workspace_id", "answer_sha256", "lease_token")},
        "worker_id": "unit-worker", **result,
    }


def public(client, run_id):
    response = client.get(f"/api/agent/runs/{run_id}/application-check", headers=GATEWAY)
    assert response.status_code == 200
    return response.json()


def test_worker_requires_admin_and_public_read_is_workspace_scoped(scenario):
    client, run_id, _, _ = scenario
    for headers in ({}, GATEWAY):
        assert client.post(f"{BASE}/claim", headers=headers, json={"worker_id": "unit-worker"}).status_code == 403
        assert client.post(f"{BASE}/enqueue/{run_id}", headers=headers).status_code == 403
        assert client.get(f"{BASE}/{run_id}", headers=headers).status_code == 403
    assert client.get(f"/api/agent/runs/{run_id}/application-check").status_code == 403
    assert public(client, run_id)["application_check"]["status"] == "unverified"
    client.cookies.set("cachet_workspace", "unit-other-cookie")
    assert client.get(f"/api/agent/runs/{run_id}/application-check", headers=GATEWAY).status_code == 404
    assert client.get(f"{BASE}/unknown", headers=ADMIN).status_code == 404


def test_exact_receipt_is_durable_idempotent_and_does_not_rewrite_done(scenario):
    client, run_id, _, _ = scenario
    job = enqueue_and_claim(scenario)
    assert job["answer"] == ANSWER and job["final_turn"] == 10
    assert job["answer_sha256"] == checks.digest(ANSWER)
    assert client.post(f"{BASE}/enqueue/{run_id}", headers=ADMIN).status_code == 200
    assert client.post(f"{BASE}/claim", headers=ADMIN, json={"worker_id": "other"}).json() == {"job": None}
    body = payload(job, receipt=receipt(job))
    result = client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=body)
    assert result.status_code == 200 and result.json()["duplicate"] is False
    result = client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=body)
    assert result.status_code == 200 and result.json()["duplicate"] is True
    observed = public(client, run_id)
    assert observed["application_check"]["status"] == "passed"
    assert observed["application_check"]["requested_format_met"] is True
    assert observed["application_check"]["checker_version"] == "docker-http-v2"
    assert observed["verified_application_turn"] == 10
    assert "answer" not in observed and "lease_token" not in observed
    assert job["lease_token"] not in str(observed)
    assert client.get(f"{BASE}/{run_id}", headers=ADMIN).json() == observed
    with session_scope(write=False) as db:
        run = db.get(AgentRun, run_id)
        assert finance.saved_application_check(db, run) == observed
        original = db.scalar(select(RunEvent.payload).where(RunEvent.run_id == run_id))
        assert original["application_check"]["status"] == "unverified"
        assert db.scalar(select(func.count()).select_from(RunEvent)) == 1
        assert db.scalar(select(func.count()).select_from(Control)) == 1
    conflict = payload(job, receipt=receipt(job, status="failed"))
    assert client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=conflict).status_code == 409


@pytest.mark.parametrize("field,value", [
    ("lease_token", "x" * 43), ("workspace_id", "another-workspace"),
    ("answer_sha256", "0" * 64), ("worker_id", "another-worker"),
])
def test_cross_identity_or_hash_result_rejected(scenario, field, value):
    client, run_id, _, _ = scenario
    job = enqueue_and_claim(scenario)
    body = payload(job, receipt=receipt(job))
    body[field] = value
    assert client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=body).status_code == 409
    assert public(client, run_id)["application_check"]["status"] == "checking"
    assert client.post(f"{BASE}/{uuid4().hex}/result", headers=ADMIN, json=payload(job, receipt=receipt(job))).status_code == 404


@pytest.mark.parametrize("change", ["answer", "source", "format", "quick_http", "extra"])
def test_receipt_must_match_source_format_and_full_http_contract(scenario, change):
    client, run_id, _, _ = scenario
    job = enqueue_and_claim(scenario)
    check = receipt(job)
    if change == "answer":
        check["answer_sha256"] = "0" * 64
    elif change == "source":
        check["source_sha256"] = "0" * 64
    elif change == "format":
        check["requested_format_met"] = False
    elif change == "quick_http":
        check["http"]["status_codes"] = [200]
    else:
        check["claimed_admin"] = True
    result = client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=payload(job, receipt=check))
    assert result.status_code == 422
    assert public(client, run_id)["application_check"]["status"] == "checking"


def test_http_pass_with_wrong_final_format_is_not_green(scenario):
    client, run_id, _, _ = scenario
    with session_scope() as db:
        last = db.scalar(select(Call).where(Call.run_id == run_id, Call.turn == 10))
        last.output_text = "Extra prose\n" + ANSWER
    job = enqueue_and_claim(scenario)
    result = client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=payload(job, receipt=receipt(job)))
    assert result.status_code == 200
    result = public(client, run_id)
    assert result["application_check"]["status"] == "failed"
    assert result["application_check"]["http"] == HTTP
    assert result["application_check"]["requested_format_met"] is False
    assert result["verified_application_turn"] is None
    assert "format" in result["application_check"]["reason"]


def test_real_failure_is_terminal_not_silently_retried(scenario):
    client, run_id, _, _ = scenario
    job = enqueue_and_claim(scenario)
    result = client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=payload(job, receipt=receipt(job, status="failed")))
    assert result.status_code == 200
    assert public(client, run_id)["application_check"]["status"] == "failed"
    assert client.post(f"{BASE}/claim", headers=ADMIN, json={"worker_id": "unit-worker"}).json() == {"job": None}


@pytest.mark.parametrize("expiration", [False, True])
def test_checker_failures_and_expired_leases_are_bounded(scenario, expiration):
    client, run_id, _, now = scenario
    job = enqueue_and_claim(scenario)
    for attempt in range(1, 4):
        assert job["attempt"] == attempt
        if expiration:
            now[0] += checks.LEASE_SECONDS + 1
            assert client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=payload(job, receipt=receipt(job))).status_code == 409
        else:
            body = payload(job, error="Synthetic Docker infrastructure failure")
            assert client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=body).status_code == 200
            assert client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=body).json()["duplicate"] is True
        job = client.post(f"{BASE}/claim", headers=ADMIN, json={"worker_id": "unit-worker"}).json()["job"]
    assert job is None
    observed = public(client, run_id)
    assert observed["application_check"]["status"] == "unverified"
    assert observed["verified_application_turn"] is None
    assert "Synthetic Docker" not in str(observed)


def test_changed_final_answer_invalidates_old_result_and_readback(scenario):
    client, run_id, _, _ = scenario
    job = enqueue_and_claim(scenario)
    with session_scope() as db:
        last = db.scalar(select(Call).where(Call.run_id == run_id, Call.turn == 10))
        last.output_text = ANSWER + "\n"
    assert client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=payload(job, receipt=receipt(job))).status_code == 409
    assert public(client, run_id)["application_check"]["status"] == "unverified"
    assert client.post(f"{BASE}/enqueue/{run_id}", headers=ADMIN).status_code == 200
    next_job = client.post(f"{BASE}/claim", headers=ADMIN, json={"worker_id": "unit-worker"}).json()["job"]
    assert next_job["answer_sha256"] != job["answer_sha256"]
    assert next_job["lease_token"] != job["lease_token"]


@pytest.mark.parametrize("change", ["running", "replay", "custom", "followup", "sponsor", "soak", "missing_turn"])
def test_only_full_ordinary_completed_builtins_are_queued(scenario, change):
    client, run_id, _, _ = scenario
    with session_scope() as db:
        run = db.get(AgentRun, run_id)
        if change == "running":
            run.status = "running"
        elif change == "replay":
            run.mode = "replay"
        elif change == "custom":
            run.task_id = "custom"
        elif change == "followup":
            run.quote_json = {"guided_followup_of": "parent"}
        elif change == "sponsor":
            run.quote_json = {"sponsor_sequence": 0}
        elif change == "soak":
            run.quote_json = {"operator_soak": True}
        else:
            db.delete(db.scalar(select(Call).where(Call.run_id == run_id, Call.turn == 10)))
        assert checks.enqueue(db, run) is None
    assert client.post(f"{BASE}/enqueue/{run_id}", headers=ADMIN).status_code == 409
    assert client.post(f"{BASE}/claim", headers=ADMIN, json={"worker_id": "unit-worker"}).json() == {"job": None}


def test_unattended_queue_does_not_claim_checking_forever(scenario):
    client, run_id, _, now = scenario
    assert client.post(f"{BASE}/enqueue/{run_id}", headers=ADMIN).status_code == 200
    now[0] += checks.UNATTENDED_SECONDS + 1
    assert public(client, run_id)["application_check"]["status"] == "unverified"
    # Returning workers can still safely resume an unclaimed job.
    job = client.post(f"{BASE}/claim", headers=ADMIN, json={"worker_id": "unit-worker"}).json()["job"]
    assert job["attempt"] == 1
    assert public(client, run_id)["application_check"]["status"] == "checking"


def test_expired_worker_does_not_leave_checking_forever(scenario):
    client, run_id, _, now = scenario
    enqueue_and_claim(scenario)
    now[0] += checks.UNATTENDED_SECONDS + 1
    assert public(client, run_id)["application_check"]["status"] == "unverified"


def test_two_concurrent_workers_cannot_claim_the_same_answer(scenario):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client, run_id, _, _ = scenario
    assert client.post(f"{BASE}/enqueue/{run_id}", headers=ADMIN).status_code == 200
    barrier = Barrier(2)

    def worker(number):
        barrier.wait()
        with session_scope() as db:
            return checks.claim(db, f"concurrent-{number}")["job"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(worker, [1, 2]))
    assert sum(job is not None for job in jobs) == 1
    assert next(job for job in jobs if job)["attempt"] == 1


def test_completed_receipt_survives_reconnect_and_rejects_changed_answer(scenario):
    from backend.app.db import init_db
    client, run_id, _, _ = scenario
    job = enqueue_and_claim(scenario)
    assert client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=payload(job, receipt=receipt(job))).status_code == 200
    init_db().dispose()
    assert public(client, run_id)["application_check"]["status"] == "passed"
    with session_scope() as db:
        last = db.scalar(select(Call).where(Call.run_id == run_id, Call.turn == 10))
        last.output_text += "\n# A different final answer"
    observed = public(client, run_id)
    assert observed["application_check"]["status"] == "unverified"
    assert observed["verified_application_turn"] is None


def test_same_answer_in_another_run_cannot_reuse_a_lease(scenario):
    client, run_id, workspace_id, _ = scenario
    first = enqueue_and_claim(scenario)
    with session_scope() as db:
        original = db.get(AgentRun, run_id)
        other = AgentRun(
            workspace_id=workspace_id, mandate_id=original.mandate_id,
            task_id=original.task_id, task_text=original.task_text,
            status="completed", mode="live", quote_json={},
        )
        db.add(other)
        db.flush()
        columns = [column.name for column in Call.__table__.columns if column.name not in {"id", "run_id", "created_at"}]
        for old in db.scalars(select(Call).where(Call.run_id == run_id)):
            db.add(Call(run_id=other.id, **{name: getattr(old, name) for name in columns}))
        checks.enqueue(db, other, required=True)
    other_job = client.post(f"{BASE}/claim", headers=ADMIN, json={"worker_id": "unit-worker"}).json()["job"]
    assert other_job["answer_sha256"] == first["answer_sha256"]
    assert other_job["run_id"] == other.id
    assert client.post(f"{BASE}/{other.id}/result", headers=ADMIN, json=payload(first, receipt=receipt(first))).status_code == 409
    assert public(client, other.id)["application_check"]["status"] == "checking"


@pytest.mark.parametrize("reason", [None, {}, "", "x" * 401])
def test_invalid_receipt_reason_returns_422_not_500(scenario, reason):
    client, run_id, _, _ = scenario
    job = enqueue_and_claim(scenario)
    check = receipt(job)
    check["reason"] = reason
    result = client.post(f"{BASE}/{run_id}/result", headers=ADMIN, json=payload(job, receipt=check))
    assert result.status_code == 422
    assert public(client, run_id)["application_check"]["status"] == "checking"


def test_new_live_done_event_starts_checking_without_rewriting_history(scenario):
    from backend.app.api.agent import completed_run_payload, emit

    _, run_id, _, _ = scenario
    with session_scope() as db:
        run = db.get(AgentRun, run_id)
        new_payload = completed_run_payload(db, run)
        emit(db, run_id, "done", new_payload)
    assert new_payload["status"] == "completed"
    assert new_payload["application_check"]["status"] == "checking"
    assert new_payload["answer_sha256"] == checks.digest(ANSWER)
    assert new_payload["verified_application_turn"] is None
    with session_scope(write=False) as db:
        history = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
        assert len(history) == 2
        assert {item.payload["application_check"]["status"] for item in history} == {
            "unverified", "checking",
        }
        assert db.get(Control, checks.PREFIX + run_id).data["state"] == "queued"


def test_replay_done_keeps_checked_archive_evidence(scenario):
    from backend.app.api.agent import completed_run_payload
    from backend.app.replay.recordings import application_evidence, load

    _, run_id, _, _ = scenario
    track = load("route-class", True)
    with session_scope() as db:
        run = db.get(AgentRun, run_id)
        run.mode = "replay"
        observed = completed_run_payload(db, run, track)
        assert observed == {
            "run_id": run_id, "status": "completed", **application_evidence(track),
        }
        assert db.get(Control, checks.PREFIX + run_id) is None


def test_queue_failure_rolls_back_only_verification_and_preserves_completion(scenario, monkeypatch):
    from backend.app.api.agent import completed_run_payload, emit

    _, run_id, _, _ = scenario
    with session_scope() as db:
        db.get(AgentRun, run_id).status = "running"

    def failing_enqueue(db, run):
        db.add(Control(key=checks.PREFIX + run.id, data={"state": "partial"}))
        db.flush()
        raise RuntimeError("Private database detail must not enter the SSE payload")

    monkeypatch.setattr(checks, "enqueue", failing_enqueue)
    with session_scope() as db:
        run = db.get(AgentRun, run_id)
        run.status = "completed"
        observed = completed_run_payload(db, run)
        emit(db, run_id, "done", observed)
        assert observed["status"] == "completed"
        assert observed["application_check"]["status"] == "unverified"
        assert "Private database" not in str(observed)
    with session_scope(write=False) as db:
        assert db.get(AgentRun, run_id).status == "completed"
        assert db.get(Control, checks.PREFIX + run_id) is None
        assert db.scalar(select(func.count()).select_from(Call)) == 10
        assert db.scalar(select(func.sum(Call.charge_micros))) == 0
        assert db.scalar(select(func.count()).select_from(Mandate)) == 1
        assert db.scalar(select(func.count()).select_from(RunEvent)) == 2
