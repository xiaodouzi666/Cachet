"""Verify leased live-run answers with the existing isolated Docker HTTP checker."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.agents.tasks import STEPS, TASKS, app_code
from backend.app.config import value
from scripts.check_generated_apps import IMAGE, assess

DIRECTORY = ROOT / "logs/application-checker"
SESSION = "cachet-app-checker"
STOP = threading.Event()
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MIN_LEASE_SECONDS = 120


class InvalidJob(ValueError):
    """The claim cannot be safely bound to a completed built-in task."""


class VerifierUnavailable(RuntimeError):
    """Infrastructure or the checker itself failed, not the generated application."""


def log(message):
    # Call sites use fixed messages, IDs, and exception classes only: no answers/tokens.
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"), message, flush=True)


def configuration(read=value):
    """Use the same trusted origin and credential selection as gpu_agent.py."""
    api_url = read("PUBLIC_API_URL").rstrip("/")
    try:
        parsed = urlsplit(api_url)
        parsed.port  # Reject malformed ports before a credentialed request.
        valid = (
            parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment and not parsed.path
            and (parsed.scheme == "https" or (
                parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
            ))
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Set PUBLIC_API_URL to the trusted Cachet backend origin")
    token = read("ADMIN_TOKEN") if parsed.hostname in {"127.0.0.1", "localhost"} else (
        read("PRODUCTION_ADMIN_TOKEN") or read("ADMIN_TOKEN")
    )
    if not token:
        raise ValueError("Configure the backend admin credential before starting the checker")
    return api_url, token


def atomic_json(path, payload):
    """Keep the exact result across lost responses and process restarts."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def instance_lock(path):
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def worker_identity(directory):
    path = directory / "worker.json"
    if not path.exists():
        atomic_json(path, {"worker_id": "cachet-app-checker-" + uuid4().hex})
    worker_id = json.loads(path.read_text())["worker_id"]
    if not isinstance(worker_id, str) or not IDENTIFIER.fullmatch(worker_id):
        raise ValueError("Invalid persisted worker identity")
    return worker_id


def validate_job(job):
    if not isinstance(job, dict):
        raise InvalidJob("Invalid claim")
    for name in ("run_id", "workspace_id"):
        if not isinstance(job.get(name), str) or not IDENTIFIER.fullmatch(job[name]):
            raise InvalidJob("Invalid job identity")
    if job.get("task_id") not in TASKS or type(job.get("final_turn")) is not int or job["final_turn"] != len(STEPS):
        raise InvalidJob("Only completed built-in final turns can be checked")
    digest = job.get("answer_sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise InvalidJob("Invalid answer digest")
    if not isinstance(job.get("answer"), str) or hashlib.sha256(job["answer"].encode()).hexdigest() != digest:
        raise InvalidJob("Claim answer does not match its digest")
    token = job.get("lease_token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", token):
        raise InvalidJob("Invalid lease")
    if type(job.get("attempt")) is not int or not 1 <= job["attempt"] <= 3:
        raise InvalidJob("Invalid attempt")
    try:
        expires = datetime.fromisoformat(job["lease_expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None:
            raise ValueError
    except (KeyError, AttributeError, TypeError, ValueError):
        raise InvalidJob("Invalid lease expiry") from None
    return expires


def docker_ready():
    """An absent image/daemon is retryable infrastructure, never failed app code."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
            capture_output=True, timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerifierUnavailable("Docker preflight unavailable") from exc
    if result.returncode:
        raise VerifierUnavailable("Docker verifier image or daemon unavailable")


def validate_receipt(job, receipt):
    if not isinstance(receipt, dict) or receipt.get("answer_sha256") != job["answer_sha256"]:
        raise VerifierUnavailable("Checker returned a mismatched answer digest")
    if receipt.get("status") not in {"passed", "failed"} or type(receipt.get("requested_format_met")) is not bool:
        raise VerifierUnavailable("Checker returned an invalid receipt")
    if receipt["status"] == "passed":
        expected = hashlib.sha256(app_code(job["answer"], allow_prose=True).encode()).hexdigest()
        if receipt.get("source_sha256") != expected or not isinstance(receipt.get("http"), dict) or not receipt["http"]:
            raise VerifierUnavailable("Checker returned unbound HTTP evidence")
    # Reject unserializable/non-finite checker results before persisting a result.
    json.dumps(receipt, allow_nan=False)


class Worker:
    def __init__(self, client, api_url, worker_id, pending_path, *, assessor=assess, preflight=docker_ready):
        self.client = client
        self.api_url = api_url
        self.worker_id = worker_id
        self.pending_path = pending_path
        self.assessor = assessor
        self.preflight = preflight

    def deliver(self):
        pending = json.loads(self.pending_path.read_text())
        payload, run_id = pending["payload"], pending["run_id"]
        if (pending.get("api_origin") != self.api_url or payload.get("worker_id") != self.worker_id
                or not isinstance(run_id, str) or not IDENTIFIER.fullmatch(run_id)):
            raise ValueError("Pending result belongs to a different worker or origin")
        response = self.client.post(
            self.api_url + f"/api/admin/application-checks/{run_id}/result", json=payload,
        )
        if response.status_code == 409:
            # Another lease/revision now owns this run; never overwrite its evidence.
            self.pending_path.unlink()
            log(f"Discarded stale or conflicting result for run {run_id}")
            return
        if response.status_code == 422:
            # Preserve the rejected payload privately, but do not let a schema mismatch
            # block all future jobs forever. The backend's attempt limit still applies.
            rejected = self.pending_path.parent / "rejected"
            rejected.mkdir(mode=0o700, exist_ok=True)
            atomic_json(rejected / f"{run_id}-{uuid4().hex}.json", pending)
            if "receipt" in payload:
                replacement = {key: payload[key] for key in (
                    "worker_id", "workspace_id", "lease_token", "answer_sha256",
                )}
                replacement["error"] = "Isolated HTTP verifier result was rejected by the backend"
                atomic_json(self.pending_path, dict(pending, payload=replacement))
                log(f"Returning rejected receipt as infrastructure failure for run {run_id}")
                self.deliver()  # At most one replacement; an error payload cannot recurse.
            else:
                self.pending_path.unlink()
                log(f"Deferred rejected infrastructure result until lease expiry for run {run_id}")
            return
        response.raise_for_status()
        acknowledgement = response.json()
        if acknowledgement.get("accepted") is not True:
            raise RuntimeError("Backend did not acknowledge application-check result")
        self.pending_path.unlink()
        status = acknowledgement.get("evidence", {}).get("application_check", {}).get("status")
        if status not in {"passed", "failed", "checking", "unverified"}:
            status = "infrastructure retry" if "error" in payload else "result"
        log(f"Acknowledged {status} for run {run_id}")

    def tick(self):
        if self.pending_path.exists():
            self.deliver()
            return True
        response = self.client.post(
            self.api_url + "/api/admin/application-checks/claim", json={"worker_id": self.worker_id},
        )
        response.raise_for_status()
        claim = response.json()
        if not isinstance(claim, dict) or "job" not in claim:
            raise InvalidJob("Invalid claim response")
        job = claim["job"]
        if job is None:
            return False
        expires = validate_job(job)
        payload = {key: job[key] for key in ("workspace_id", "lease_token", "answer_sha256")}
        payload["worker_id"] = self.worker_id
        try:
            if (expires - datetime.now(timezone.utc)).total_seconds() < MIN_LEASE_SECONDS:
                raise VerifierUnavailable("Insufficient lease time for a full HTTP check")
            self.preflight()
            # assess defaults to the full verifier, including the actual 60.1s wait.
            # It retains the no-network, read-only, non-root Docker isolation unchanged.
            receipt = self.assessor(job["task_id"], job["answer"])
            validate_receipt(job, receipt)
            if receipt["status"] == "failed":
                self.preflight()  # A disappearing Docker daemon must not fail app code.
            payload["receipt"] = receipt
        except Exception as exc:
            # The original assessor contains genuine HTTP assertion failures in receipts.
            # Exceptions escaping it indicate unavailable infrastructure/checker faults.
            payload["error"] = "Isolated HTTP verifier unavailable (" + type(exc).__name__ + ")"
        atomic_json(self.pending_path, {
            "api_origin": self.api_url, "run_id": job["run_id"], "payload": payload,
        })
        self.deliver()
        return True


def serve():
    api_url, token = configuration()
    with instance_lock(DIRECTORY / "worker.lock"):
        worker_id = worker_identity(DIRECTORY)
        # No redirects or environment proxy: the admin token goes to the configured origin only.
        with httpx.Client(headers={"X-Admin-Token": token}, timeout=8, follow_redirects=False, trust_env=False) as client:
            worker = Worker(client, api_url, worker_id, DIRECTORY / "pending.json")
            log("Application checker started")
            while not STOP.is_set():
                try:
                    worked = worker.tick()
                except Exception as exc:
                    log("Application checker retry: " + type(exc).__name__)
                    worked = False
                STOP.wait(1 if worked else 5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "serve"])
    args = parser.parse_args()
    os.chdir(ROOT)
    os.umask(0o077)
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == "start":
        configuration()  # Fail before launching a restart loop with an invalid origin/token.
        if subprocess.run(["tmux", "has-session", "-t", "=" + SESSION], capture_output=True).returncode:
            subprocess.run([
                "tmux", "new-session", "-d", "-s", SESSION, "-c", str(ROOT),
                "umask 077; while true; do .venv/bin/python scripts/application_check_worker.py serve "
                ">> logs/application-checker/worker.log 2>&1; sleep 3; done",
            ], check=True)
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: STOP.set())
        serve()


if __name__ == "__main__":
    main()
