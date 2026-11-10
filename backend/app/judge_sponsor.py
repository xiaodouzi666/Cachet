"""Fund a fresh judge workspace's verified publisher royalties with bounded sandbox traffic."""

import asyncio
import hashlib
import logging
import secrets
import time
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from threading import Thread

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import func, select
from backend.app import ledger
from backend.app.api import catalog
from backend.app.config import value
from backend.app.db import configured_url, session_scope
from backend.app.gateway.modes import health
from backend.app.models import AgentRun, Consumer, Mandate, Workspace
from backend.app.money import usd_to_micros
from backend.app.operations import Control

LOG = logging.getLogger(__name__)
JOB = "judge-preparation:"
TARGET = "sponsor-target:"
MAX_RUNS = 30
CAP = 1_000_000
TERMINAL = {"ready", "exhausted", "failed"}


def touch(db, viewer, **fields):
    job = db.get(Control, JOB + viewer)
    job.data = {**job.data, **fields}


def ensure_catalog(viewer, bg):
    parent = None
    contexts, publishers = [], []
    for slug in ("starlette", "fastapi"):
        address = value("PUBLISHER_" + slug.upper() + "_EMAIL")
        if not address:
            raise HTTPException(503, "Configure both sandbox publisher recipients")
        pub = catalog.publish_owner(
            catalog.PublisherInput(
                name=slug.capitalize() + " maintainer", paypal_email=address
            ),
            ws=viewer,
        )
        ctx = catalog.publish(
            catalog.PublishInput(
                publisher_id=pub["id"], source_slug=slug, parent_id=parent
            ),
            bg,
            ws=viewer,
        )
        publishers.append(pub["id"])
        contexts.append(ctx["id"])
        parent = ctx["id"]
    if len(set(publishers)) != 2:
        raise HTTPException(
            409, "The guided demo requires two distinct sandbox recipients"
        )
    return contexts, publishers


def prepare(viewer, bg):
    with session_scope(write=False) as db:
        if db.get(Workspace, viewer).kind != "judge":
            raise HTTPException(403, "Start a judge workspace first")
        existing = db.get(Control, JOB + viewer)
    if not existing:
        if value("PAYPAL_ENV", "sandbox") != "sandbox" or not value(
            "SANDBOX_TEST_CARD_NUMBER"
        ):
            raise HTTPException(
                503, "The guided sponsor requires a fixed sandbox test card"
            )
        contexts, publishers = ensure_catalog(viewer, bg)
        with session_scope() as db:
            if not db.get(Control, JOB + viewer):
                pending = sum(
                    r.data.get("status") not in TERMINAL
                    for r in db.scalars(
                        select(Control).where(Control.key.startswith(JOB))
                    )
                )
                if pending >= int(value("JUDGE_SPONSOR_MAX_PENDING", "12")):
                    raise HTTPException(
                        429,
                        "All sponsor preparation slots are in use. Retry this workspace shortly.",
                        headers={"Retry-After": "60"},
                    )
                # The public session never receives a cookie or mandate ID for this workspace.
                hidden = Workspace(
                    kind="sponsor",
                    cookie_id=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                )
                db.add(hidden)
                db.flush()
                while True:
                    salt = secrets.token_hex(4)
                    if not db.scalar(
                        select(Consumer.id).where(Consumer.salt_hex == salt)
                    ):
                        break
                db.add(
                    Consumer(
                        workspace_id=hidden.id,
                        name="Judge sandbox sponsor",
                        salt_hex=salt,
                    )
                )
                db.add(
                    Control(
                        key=TARGET + hidden.id,
                        data={
                            "viewer": viewer,
                            "contexts": contexts,
                            "publishers": publishers,
                        },
                    )
                )
                db.add(
                    Control(
                        key=JOB + viewer,
                        data={
                            "viewer": viewer,
                            "sponsor": hidden.id,
                            "status": "queued",
                            "failures": 0,
                            "contexts": contexts,
                            "publishers": publishers,
                            "turns": 0,
                        },
                    )
                )
    bg.add_task(kick)
    return status(viewer)


def from_db(db, viewer):
    job = db.get(Control, JOB + viewer)
    if not job:
        return {"status": "not_started"}
    data = job.data
    threshold = usd_to_micros(value("JUDGE_PAYOUT_MIN_USD", ".01"))
    money = []
    for slug, pid in zip(("starlette", "fastapi"), data["publishers"]):
        money.append(
            {
                "slug": slug,
                "available_micros": max(0, ledger.available_payable(db, viewer, pid)),
                "threshold_micros": max(10_000, threshold),
            }
        )
    return {
        **{
            k: data[k]
            for k in (
                "status",
                "turns",
                "error",
                "captured_micros",
                "forgiven_micros",
                "fee_micros",
                "completed_runs",
                "objects_checked",
            )
            if k in data
        },
        "context_ids": data["contexts"],
        "publisher_funding": money,
        "max_runs": MAX_RUNS,
        "cap_micros": CAP,
    }


def status(viewer):
    with session_scope(write=False) as db:
        return from_db(db, viewer)


def grant(db, sponsor_ws, mid, sequence, context_id, task_id):
    target = db.get(Control, TARGET + sponsor_ws)
    if not target or db.get(Workspace, sponsor_ws).kind != "sponsor":
        raise HTTPException(403, "No internal sponsor registration")
    job = db.get(Control, JOB + target.data["viewer"])
    if (
        not job
        or job.data.get("sponsor") != sponsor_ws
        or job.data.get("mandate") != mid
        or job.data["status"] in TERMINAL
        or task_id != "rate-limit"
        or context_id != target.data["contexts"][-1]
    ):
        raise HTTPException(403, "The sponsor registration does not cover this action")
    runs = list(
        db.scalars(
            select(AgentRun)
            .where(AgentRun.workspace_id == sponsor_ws)
            .order_by(AgentRun.created_at)
        )
    )
    if not 1 <= sequence <= MAX_RUNS or sequence > len(runs) + 1:
        raise HTTPException(409, "Sponsor sequence or run limit exceeded")
    if sequence <= len(runs):
        run = runs[sequence - 1]
        if run.quote_json.get("sponsor_sequence") != sequence:
            raise HTTPException(409, "Sponsor history mismatch")
        return {"run_id": run.id, "status": run.status}
    return None


def step(viewer):
    from backend.app.api.agent import Start, start_run
    from backend.app.settlement import service
    from backend.app.paypal.reconciliation import reconcile
    from backend.app.settlement.audit import audit

    with session_scope(write=False) as db:
        data = dict(db.get(Control, JOB + viewer).data)
    sw = data["sponsor"]
    if data.get("mandate"):
        mid = data["mandate"]
    else:
        if health()["mode"] != "live":
            return "wait"
        # Persist the order identity before authorize so the same real order resumes after interruption.
        m = service.open_order(sw, CAP, card=True)
        mid = m["id"]
        with session_scope() as db:
            touch(db, viewer, mandate=mid, status="preparing")
    with session_scope(write=False) as db:
        current = db.get(Mandate, mid)
        authorized = bool(current.authorization_id)
    if not authorized:
        service.authorize(sw, mid)
    with session_scope(write=False) as db:
        runs = list(
            db.scalars(
                select(AgentRun)
                .where(AgentRun.workspace_id == sw)
                .order_by(AgentRun.created_at)
            )
        )
    for r in runs:
        if r.status in ("queued", "running"):
            return "wait"
        with session_scope(write=False) as db:
            audited = db.get(Control, "sponsor-audit:" + r.id)
        if not audited:
            audit(sw, mid, run_id=r.id)
            with session_scope() as db:
                db.add(Control(key="sponsor-audit:" + r.id, data={"audited": True}))
    with session_scope() as db:
        from backend.app.models import Call

        turns = db.scalar(
            select(func.count()).select_from(Call).where(Call.workspace_id == sw)
        )
        successful = sum(r.status == "completed" for r in runs)
        touch(db, viewer, turns=turns, completed_runs=successful)
        pending = from_db(db, viewer)["publisher_funding"]
        gross = [
            -ledger.balance(db, viewer, ledger.payable(pid))
            for pid in data["publishers"]
        ]
        m = db.get(Mandate, mid)
        summary = service.view(m)
        touch(
            db,
            viewer,
            captured_micros=summary["captured_micros"],
            forgiven_micros=summary["forgiven_micros"],
            fee_micros=summary["capture_fee_micros"],
        )
    # The two fixed recipients must have spendable balances. A prepared payout must never be counted twice.
    funded = all(row["available_micros"] >= row["threshold_micros"] for row in pending)
    if funded and summary["captured_micros"] > 0:
        closed = service.close(sw, mid)
        final = reconcile(sw)
        if not final["ok"] or not final["checked"] or final["differences"]:
            raise ValueError("Sponsor sandbox receipt reconciliation is not complete")
        with session_scope() as db:
            touch(
                db,
                viewer,
                status="ready",
                error=None,
                captured_micros=closed["captured_micros"],
                forgiven_micros=closed["forgiven_micros"],
                fee_micros=closed["capture_fee_micros"],
                objects_checked=final["checked"],
            )
        return "closed"
    if summary["status"] not in ("ACTIVE", "CAPTURING"):
        with session_scope() as db:
            touch(
                db,
                viewer,
                status="exhausted",
                error="Sponsor authorization has closed.",
            )
        return "closed"
    if (
        all(v >= r["threshold_micros"] for v, r in zip(gross, pending))
        and summary["accrued_uncaptured_micros"] >= 10_000
    ):
        paid = service.capture(sw, mid, force=True)
        if paid:
            return "continue"
    if (
        len(runs) >= MAX_RUNS
        or summary["cap_micros"]
        - summary["captured_micros"]
        - summary["accrued_uncaptured_micros"]
        < 15_000
    ):
        closed = service.close(sw, mid)
        with session_scope() as db:
            actual = from_db(db, viewer)["publisher_funding"]
            status_ = (
                "ready"
                if closed["captured_micros"]
                and all(r["available_micros"] >= r["threshold_micros"] for r in actual)
                else "exhausted"
            )
            touch(
                db,
                viewer,
                status=status_,
                captured_micros=closed["captured_micros"],
                forgiven_micros=closed["forgiven_micros"],
                fee_micros=closed["capture_fee_micros"],
            )
        return "closed"
    if health()["mode"] != "live":
        return "wait"
    bg = BackgroundTasks()
    req = Start(
        mandate_id=mid,
        context_id=data["contexts"][-1],
        task_id="rate-limit",
        est_turns=10,
        cold_fraction=0.25,
    )
    start_run(req, bg, None, sw, sponsor_sequence=len(runs) + 1)
    asyncio.run(bg())
    return "continue"


def worker():
    # A durable DB job survives process restarts. An OS lock permits at most one sponsor GPU slot per persistent disk.
    url = configured_url()
    if not url.startswith("sqlite:///"):
        return
    lockfile = Path(url[len("sqlite:///") :]).resolve().with_suffix(".judge-prep.lock")
    with lockfile.open("a") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError:
            return
        while True:
            with session_scope(write=False) as db:
                jobs = [
                    j.data
                    for j in db.scalars(
                        select(Control).where(Control.key.startswith(JOB))
                    )
                    if j.data["status"] not in TERMINAL
                ]
            if not jobs:
                return
            job = min(jobs, key=lambda j: j.get("last_step", 0))
            viewer = job["viewer"]
            with session_scope() as db:
                touch(db, viewer, last_step=time.time())
            try:
                action = step(viewer)
            except Exception as exc:
                LOG.exception(
                    "Sponsor preparation failed for a registered judge workspace"
                )
                with session_scope() as db:
                    n = int(db.get(Control, JOB + viewer).data.get("failures", 0)) + 1
                    touch(
                        db,
                        viewer,
                        failures=n,
                        status="failed" if n >= 3 else "retrying",
                        error=type(exc).__name__,
                    )
                if n >= 3 and job.get("mandate"):
                    from backend.app.settlement import service

                    try:
                        service.close(job["sponsor"], job["mandate"])
                    except Exception:
                        LOG.exception("Could not close the sponsor authorization")
                return
            if action == "wait":
                return


def kick():
    Thread(target=worker, daemon=True, name="judge-sponsor").start()
