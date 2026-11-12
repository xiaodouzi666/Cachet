"""Worker regressions: immutable answers, infrastructure, and lost acknowledgements."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.app.agents.tasks import app_code
from scripts import application_check_worker as checker

ORIGIN = "https://cachet-api.example.test"
ANSWER = "```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```"


def job():
    return {
        "run_id": "a" * 32, "workspace_id": "b" * 32, "task_id": "rate-limit",
        "final_turn": 10, "answer": ANSWER,
        "answer_sha256": hashlib.sha256(ANSWER.encode()).hexdigest(),
        "lease_token": "c" * 48,
        "lease_expires_at": (datetime.now(timezone.utc) + timedelta(seconds=180)).isoformat(),
        "attempt": 1,
    }


def receipt():
    return {
        "answer_sha256": hashlib.sha256(ANSWER.encode()).hexdigest(),
        "requested_format_met": True, "status": "passed",
        "source_sha256": hashlib.sha256(app_code(ANSWER, allow_prose=True).encode()).hexdigest(),
        "http": {"status_codes": [200, 200, 429, 200], "distinct_clients": 2,
                 "window_boundary_seconds": 60.1, "rolling_status_codes": [200, 200, 429, 429, 200]},
    }


def make_worker(tmp_path, handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return checker.Worker(client, ORIGIN, "unit-worker", tmp_path / "pending.json", **kwargs)


@pytest.mark.parametrize("origin", [
    "", "http://cachet.example", "https://user:secret@cachet.example", "https://cachet.example/path",
    "https://cachet.example?secret=x", "https://cachet.example#fragment", "file:///tmp/a",
    "https://cachet.example:bad", "https:///missing", "https://[bad",
])
def test_untrusted_origins_are_rejected_without_exposing_credentials(origin):
    with pytest.raises(ValueError, match="trusted Cachet backend origin") as error:
        checker.configuration({"PUBLIC_API_URL": origin}.get)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("origin,expected", [
    ("http://localhost:8000/", "local"), ("http://127.0.0.1:8000", "local"),
    ("https://localhost", "local"), (ORIGIN + "/", "production"),
])
def test_credentials_match_gpu_agent_origin_selection(origin, expected):
    settings = {"PUBLIC_API_URL": origin, "ADMIN_TOKEN": "local", "PRODUCTION_ADMIN_TOKEN": "production"}
    assert checker.configuration(settings.get) == (origin.rstrip("/"), expected)


def test_production_fallback_and_missing_token():
    assert checker.configuration({"PUBLIC_API_URL": ORIGIN, "ADMIN_TOKEN": "fallback"}.get)[1] == "fallback"
    with pytest.raises(ValueError, match="admin credential"):
        checker.configuration({"PUBLIC_API_URL": ORIGIN}.get)


@pytest.mark.parametrize("field,replacement", [
    ("run_id", "../some-other-run"), ("workspace_id", "../../"), ("task_id", "custom"),
    ("final_turn", 9), ("final_turn", True), ("answer_sha256", "0" * 64),
    ("answer", ANSWER + "changed"), ("lease_token", "bad/token"),
    ("lease_expires_at", "2026-11-12T00:00:00"), ("lease_expires_at", "invalid"),
    ("attempt", 4),
])
def test_invalid_claim_cannot_execute_or_submit(tmp_path, field, replacement):
    claimed, requests, calls = job(), [], []
    claimed[field] = replacement

    def handle(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"job": claimed})

    worker = make_worker(tmp_path, handle, assessor=lambda *a: calls.append(a), preflight=lambda: None)
    with pytest.raises(checker.InvalidJob):
        worker.tick()
    assert not calls and len(requests) == 1 and not worker.pending_path.exists()


def test_full_assess_receipt_is_bound_to_exact_claim(tmp_path):
    claimed, calls, payloads = job(), [], []

    def assess(task, answer):
        calls.append((task, answer))
        return receipt()

    def handle(request):
        if request.url.path.endswith("/claim"):
            assert json.loads(request.content) == {"worker_id": "unit-worker"}
            return httpx.Response(200, json={"job": claimed})
        assert request.url.path.endswith("/" + claimed["run_id"] + "/result")
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True, "duplicate": False})

    worker = make_worker(tmp_path, handle, assessor=assess, preflight=lambda: None)
    assert worker.tick()
    assert calls == [("rate-limit", ANSWER)]  # No quick/draft mode, no rewritten answer.
    assert payloads == [{
        "worker_id": "unit-worker", "workspace_id": claimed["workspace_id"],
        "lease_token": claimed["lease_token"], "answer_sha256": claimed["answer_sha256"],
        "receipt": receipt(),
    }]
    assert not worker.pending_path.exists()


def test_lost_acknowledgement_reuses_identical_payload_after_restart(tmp_path, capsys):
    claimed, assessments, submissions = job(), [], []

    def handle(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": claimed})
        submissions.append(request.content)
        if len(submissions) == 1:
            raise httpx.ReadError("Do not log this sensitive remote response", request=request)
        return httpx.Response(200, json={"accepted": True, "duplicate": True})

    def assess(*args):
        assessments.append(args)
        return receipt()

    first = make_worker(tmp_path, handle, assessor=assess, preflight=lambda: None)
    with pytest.raises(httpx.ReadError):
        first.tick()
    saved = first.pending_path.read_text()
    assert ANSWER not in saved and '"answer"' not in saved
    assert first.pending_path.stat().st_mode & 0o777 == 0o600
    restarted = make_worker(tmp_path, handle, assessor=assess, preflight=lambda: None)
    assert restarted.tick()
    assert len(assessments) == 1 and submissions[0] == submissions[1]
    assert not restarted.pending_path.exists()
    output = capsys.readouterr().out
    assert claimed["lease_token"] not in output and ANSWER not in output and "sensitive" not in output


@pytest.mark.parametrize("status", [302, 401, 403, 503])
def test_failed_result_transport_keeps_journal_and_does_not_follow_redirect(tmp_path, status):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job()})
        return httpx.Response(status, headers={"Location": "https://untrusted.example/"})

    worker = make_worker(tmp_path, handle, assessor=lambda *a: receipt(), preflight=lambda: None)
    with pytest.raises(httpx.HTTPStatusError):
        worker.tick()
    assert worker.pending_path.exists() and len(paths) == 2


@pytest.mark.parametrize("reject_infrastructure", [False, True])
def test_rejected_schema_is_archived_and_does_not_block_later_claims(tmp_path, reject_infrastructure):
    claims, payloads = [], []

    def handle(request):
        if request.url.path.endswith("/claim"):
            claims.append(True)
            return httpx.Response(200, json={"job": job() if len(claims) == 1 else None})
        payloads.append(json.loads(request.content))
        if len(payloads) == 1 or reject_infrastructure:
            return httpx.Response(422, json={"detail": "Sensitive response must not be logged"})
        return httpx.Response(200, json={"accepted": True, "evidence": {"application_check": {"status": "checking"}}})

    worker = make_worker(tmp_path, handle, assessor=lambda *a: receipt(), preflight=lambda: None)
    assert worker.tick()
    assert not worker.pending_path.exists()
    assert worker.tick() is False and len(claims) == 2
    assert len(payloads) == 2 and "receipt" in payloads[0] and "receipt" not in payloads[1]
    assert payloads[1]["error"] == "Isolated HTTP verifier result was rejected by the backend"
    for field in ("worker_id", "workspace_id", "lease_token", "answer_sha256"):
        assert payloads[0][field] == payloads[1][field]
    archived = list((tmp_path / "rejected").glob("*.json"))
    assert len(archived) == (2 if reject_infrastructure else 1)
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in archived)
    assert (tmp_path / "rejected").stat().st_mode & 0o777 == 0o700
    originals = [json.loads(path.read_text())["payload"] for path in archived]
    assert payloads[0] in originals


def test_log_uses_backend_final_status_after_format_rejection(tmp_path, capsys):
    observed = receipt()
    observed["requested_format_met"] = False

    def handle(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job()})
        return httpx.Response(200, json={"accepted": True, "evidence": {"application_check": {"status": "failed"}}})

    worker = make_worker(tmp_path, handle, assessor=lambda *a: observed, preflight=lambda: None)
    worker.tick()
    output = capsys.readouterr().out
    assert "Acknowledged failed" in output and "Acknowledged passed" not in output


def test_expired_or_conflicting_receipt_is_discarded_without_rechecking(tmp_path):
    calls = []

    def handle(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job()})
        return httpx.Response(409, json={"detail": "Lease expired"})

    def assess(*args):
        calls.append(args)
        return receipt()

    worker = make_worker(tmp_path, handle, assessor=assess, preflight=lambda: None)
    assert worker.tick() and len(calls) == 1 and not worker.pending_path.exists()


@pytest.mark.parametrize("failure", ["preflight", "assess", "postflight", "answer_hash", "source_hash"])
def test_infrastructure_and_unbound_receipts_never_report_app_failure(tmp_path, failure):
    payloads, checks = [], []

    def handle(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job()})
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    def preflight():
        checks.append(True)
        if failure == "preflight" or (failure == "postflight" and len(checks) == 2):
            raise checker.VerifierUnavailable("Do not expose Docker stderr")

    def assess(*args):
        if failure == "assess":
            raise RuntimeError("Docker could not start; secret must not be logged")
        observed = receipt()
        if failure == "postflight":
            observed = {"status": "failed", "requested_format_met": True,
                        "answer_sha256": observed["answer_sha256"], "reason": "Container exit code 137"}
        elif failure == "answer_hash":
            observed["answer_sha256"] = "0" * 64
        elif failure == "source_hash":
            observed["source_sha256"] = "0" * 64
        return observed

    worker = make_worker(tmp_path, handle, assessor=assess, preflight=preflight)
    worker.tick()
    assert len(payloads) == 1 and "receipt" not in payloads[0]
    assert payloads[0]["error"].startswith("Isolated HTTP verifier unavailable (")
    assert "secret" not in payloads[0]["error"] and "stderr" not in payloads[0]["error"]


def test_genuine_http_failure_is_preserved_not_retried(tmp_path):
    failed = {"answer_sha256": job()["answer_sha256"], "status": "failed",
              "requested_format_met": True, "reason": "AssertionError: rejected requests consume quota"}
    received, checks = [], []

    def handle(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job()})
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    worker = make_worker(tmp_path, handle, assessor=lambda *a: deepcopy(failed), preflight=lambda: checks.append(True))
    worker.tick()
    assert received[0]["receipt"] == failed and "error" not in received[0] and len(checks) == 2


def test_nearly_expired_lease_is_returned_as_infrastructure_without_running(tmp_path):
    claimed, calls, payloads = job(), [], []
    claimed["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()

    def handle(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": claimed})
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    worker = make_worker(tmp_path, handle, assessor=lambda *a: calls.append(a), preflight=lambda: calls.append("docker"))
    worker.tick()
    assert not calls and "receipt" not in payloads[0] and "error" in payloads[0]


def test_empty_queue_and_persisted_origin_boundary(tmp_path):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"job": None})

    worker = make_worker(tmp_path, handle, preflight=lambda: pytest.fail("No Docker without a job"))
    assert worker.tick() is False and len(requests) == 1
    checker.atomic_json(worker.pending_path, {"api_origin": "https://different.example",
        "run_id": "a" * 32, "payload": {"worker_id": "unit-worker"}})
    with pytest.raises(ValueError, match="different worker or origin"):
        worker.tick()
    assert len(requests) == 1 and worker.pending_path.exists()


def test_single_instance_lock_and_persistent_identity(tmp_path):
    with checker.instance_lock(tmp_path / "worker.lock"):
        with pytest.raises(BlockingIOError):
            with checker.instance_lock(tmp_path / "worker.lock"):
                pytest.fail("A duplicate worker acquired the lock")
        identity = checker.worker_identity(tmp_path)
        assert checker.worker_identity(tmp_path) == identity
    with checker.instance_lock(tmp_path / "worker.lock"):
        assert checker.worker_identity(tmp_path) == identity


def test_docker_image_or_daemon_error_is_infrastructure(monkeypatch):
    observed = []

    def run(command, **kwargs):
        observed.append((command, kwargs))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(checker.subprocess, "run", run)
    with pytest.raises(checker.VerifierUnavailable):
        checker.docker_ready()
    command, options = observed[0]
    assert command == ["docker", "image", "inspect", checker.IMAGE, "--format", "{{.Id}}"]
    assert options["capture_output"] is True and options["timeout"] == 8
