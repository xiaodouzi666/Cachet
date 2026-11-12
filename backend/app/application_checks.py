"""Durable, hash-bound independent application checks; never execute user code here."""

from datetime import datetime, timezone
import hashlib
import json
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import select

from backend.app.agents.tasks import STEPS, TASKS, app_code
from backend.app.models import AgentRun, Call
from backend.app.operations import Control
from backend.app.replay.recordings import validate_application

PREFIX = "application-check:"
LEASE_SECONDS = 180
MAX_ATTEMPTS = 3
UNATTENDED_SECONDS = 900
RECEIPT_FIELDS = {
    "answer_sha256", "source_sha256", "requested_format_met", "status", "http", "reason",
}


def clock():
    return time.time()


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def target(db, run):
    """Return only a completed, full built-in answer eligible for independent checking."""
    quote = run.quote_json or {}
    if (
        run.status != "completed" or run.task_id not in TASKS
        or run.mode not in {"live", "hosted"}
        or quote.get("guided_followup_of") or quote.get("operator_soak")
        or "sponsor_sequence" in quote
    ):
        return None
    calls = list(db.scalars(select(Call).where(
        Call.run_id == run.id, Call.workspace_id == run.workspace_id
    ).order_by(Call.turn)))
    if [call.turn for call in calls] != list(range(1, len(STEPS) + 1)):
        return None
    final = calls[-1]
    if not final.output_text or len(final.output_text.encode()) > 100_000:
        return None
    return {
        "run_id": run.id, "workspace_id": run.workspace_id,
        "task_id": run.task_id, "final_call_id": final.id,
        "final_turn": final.turn, "answer_sha256": digest(final.output_text),
        "answer": final.output_text,
    }


def matches(data, current):
    return current is not None and all(data.get(key) == current[key] for key in (
        "run_id", "workspace_id", "task_id", "final_call_id", "final_turn", "answer_sha256",
    ))


def enqueue(db, run, *, required=False):
    current = target(db, run)
    if current is None:
        if required:
            raise HTTPException(409, "Only complete ordinary built-in live runs can be checked")
        return None
    key = PREFIX + run.id
    row = db.get(Control, key)
    if row is not None and matches(row.data, current):
        return row
    now = clock()
    data = {
        **{key: value for key, value in current.items() if key != "answer"},
        "version": 1, "checker_version": "docker-http-v2", "state": "queued", "attempts": 0,
        "created_at": now, "updated_at": now,
    }
    if row is None:
        row = Control(key=key, data=data)
        db.add(row)
    else:
        # Replacing an obsolete answer invalidates its receipt and every old lease.
        row.data = data
    return row


def _unknown(run, reason, current=None, status="unverified"):
    return {
        "run_id": run.id,
        "answer_sha256": current["answer_sha256"] if current else None,
        "application_check": {"status": status, "reason": reason},
        "verified_application_turn": None,
    }


def saved_evidence(db, run):
    """None means no independent job exists; callers may retain validated replay evidence."""
    row = db.get(Control, PREFIX + run.id)
    if row is None:
        return None
    current = target(db, run)
    data = row.data
    if not matches(data, current):
        return _unknown(run, "The saved check does not match this run's final answer.", current)
    if data.get("state") == "done":
        check = data.get("receipt", {})
        try:
            check = _validated_receipt(run.task_id, check, current["answer"])
        except (HTTPException, ValueError, KeyError, TypeError):
            return _unknown(run, "The saved independent check is invalid.", current)
        passed = check.get("status") == "passed" and check.get("requested_format_met") is True
        return {
            "run_id": run.id, "answer_sha256": current["answer_sha256"],
            "application_check": {**check, "checked_at": iso(data["completed_at"]),
                                  "checker_version": data["checker_version"]},
            "verified_application_turn": current["final_turn"] if passed else None,
        }
    now = clock()
    exhausted = data.get("attempts", 0) >= MAX_ATTEMPTS and (
        data.get("state") != "running" or data.get("lease_expires_at", 0) <= now
    )
    if data.get("state") == "unverified" or exhausted:
        return _unknown(run, "The independent checker could not finish after three attempts.", current)
    if (data.get("state") != "running" or data.get("lease_expires_at", 0) <= now) and now - data["created_at"] > UNATTENDED_SECONDS:
        return _unknown(run, "The independent checker is unavailable; no passing result is claimed.", current)
    return _unknown(run, "Independent Docker HTTP verification is queued or running.", current, "checking")


def evidence(db, run):
    return saved_evidence(db, run) or _unknown(
        run, "This live run has no independent HTTP verification.", target(db, run)
    )


def claim(db, worker_id):
    now = clock()
    rows = list(db.scalars(select(Control).where(Control.key.startswith(PREFIX))))
    rows.sort(key=lambda row: (row.data.get("created_at", 0), row.key))
    for row in rows:
        data = dict(row.data)
        if data.get("state") not in {"queued", "running"}:
            continue
        if data["state"] == "running" and data.get("lease_expires_at", 0) > now:
            continue
        run = db.get(AgentRun, data.get("run_id"))
        current = target(db, run) if run else None
        if not matches(data, current):
            row.data = {**data, "state": "unverified", "updated_at": now}
            continue
        if data.get("attempts", 0) >= MAX_ATTEMPTS:
            row.data = {**data, "state": "unverified", "updated_at": now}
            continue
        token = secrets.token_urlsafe(32)
        data.update(
            state="running", worker_id=worker_id, lease_hash=digest(token),
            lease_expires_at=now + LEASE_SECONDS, attempts=data.get("attempts", 0) + 1,
            updated_at=now,
        )
        data.pop("result_digest", None)
        row.data = data
        return {"job": {
            **{key: value for key, value in current.items() if key != "final_call_id"},
            "lease_token": token, "lease_expires_at": iso(data["lease_expires_at"]),
            "attempt": data["attempts"],
        }}
    return {"job": None}


def _validated_receipt(task_id, receipt, answer):
    if not isinstance(receipt, dict) or set(receipt) - RECEIPT_FIELDS:
        raise HTTPException(422, "Unexpected independent checker receipt fields")
    if "reason" in receipt and (
        not isinstance(receipt["reason"], str) or not receipt["reason"].strip()
        or len(receipt["reason"]) > 400
    ):
        raise HTTPException(422, "Checker reason must be nonempty text of at most 400 characters")
    try:
        validate_application(task_id, receipt, answer)
        if "source_sha256" in receipt:
            if receipt["source_sha256"] != digest(app_code(answer, allow_prose=True)):
                raise ValueError("Source digest differs")
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, "Checker receipt does not match the final answer or HTTP contract") from exc
    result = dict(receipt)
    if result["status"] == "passed" and result["requested_format_met"] is not True:
        result.update(
            status="failed",
            reason="The HTTP contract passed, but the final answer did not meet the requested code-only format.",
        )
    if "reason" in result:
        result["reason"] = result["reason"][:400]
    return result


def submit(db, run_id, payload):
    row = db.get(Control, PREFIX + run_id)
    run = db.get(AgentRun, run_id)
    if row is None or run is None:
        raise HTTPException(404, "Application check was not found")
    data = dict(row.data)
    current = target(db, run)
    if (
        not matches(data, current) or payload["workspace_id"] != run.workspace_id
        or payload["answer_sha256"] != current["answer_sha256"]
        or data.get("worker_id") != payload["worker_id"]
        or not secrets.compare_digest(data.get("lease_hash", ""), digest(payload["lease_token"]))
    ):
        raise HTTPException(409, "Application check lease or final answer differs")
    receipt, error = payload.get("receipt"), payload.get("error")
    if (receipt is None) == (error is None):
        raise HTTPException(422, "Provide exactly one checker receipt or infrastructure error")
    result_digest = digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    if data.get("result_digest") == result_digest:
        return {"accepted": True, "duplicate": True, "evidence": evidence(db, run)}
    now = clock()
    if data.get("state") != "running" or data.get("lease_expires_at", 0) <= now:
        raise HTTPException(409, "Application check lease has expired or already completed")
    if error is not None:
        data.update(
            state="queued" if data["attempts"] < MAX_ATTEMPTS else "unverified",
            last_error=error[:400],
        )
    else:
        checked = _validated_receipt(run.task_id, receipt, current["answer"])
        data.update(state="done", receipt=checked, completed_at=now)
    data.update(updated_at=now, result_digest=result_digest)
    row.data = data
    return {"accepted": True, "duplicate": False, "evidence": evidence(db, run)}
