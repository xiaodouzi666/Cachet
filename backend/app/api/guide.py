"""Derive six judge milestones from this workspace's saved metering and provider receipts."""

from datetime import datetime, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from backend.app import judge_sponsor, ledger
from backend.app.api.access import gateway, current_workspace, owned
from backend.app.api.agent import Start, start_run
from backend.app.db import session_scope
from backend.app.models import (
    AgentRun,
    Call,
    Capture,
    Context,
    Mandate,
    PayoutBatch,
    PayoutItem,
    Refund,
    Workspace,
)
from backend.app.operations import Control
from backend.app.paypal.reconciliation import reconcile
from backend.app.settlement import service
from backend.app.settlement.payout_job import settle

router = APIRouter(prefix="/api/judge", dependencies=[Depends(gateway)])
wsdep = Depends(current_workspace)


class GuideTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mandate_id: str
    task_id: str


class ContinueWith(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mandate_id: str


def run_view(row):
    if row is None:
        return None
    return {
        "id": row.id,
        "mandate_id": row.mandate_id,
        "task_id": row.task_id,
        "status": row.status,
        "mode": row.mode,
        "quote_json": row.quote_json,
    }


def close_view(db, ws, mid):
    record = db.get(Control, "judge-finish:" + mid)
    if not record:
        return {}
    data = record.data
    b = db.get(PayoutBatch, data["batch_id"]) if data.get("batch_id") else None
    if not b or b.workspace_id != ws:
        return {"status": data.get("status")}
    items = [
        {
            "id": r.paypal_item_id,
            "publisher_id": r.publisher_id,
            "amount_cents": r.amount_cents,
            "fee_cents": r.fee_cents,
            "status": r.status,
        }
        for r in db.scalars(
            select(PayoutItem).where(
                PayoutItem.batch_id == b.id, PayoutItem.workspace_id == ws
            )
        )
    ]
    return {
        "batch_id": b.id,
        "paypal_batch_id": b.paypal_batch_id,
        "status": b.status,
        "items": items,
        "reconciliation": data.get("reconciliation"),
    }


@router.get("/walkthrough")
def walkthrough(ws=wsdep):
    with session_scope(write=False) as db:
        if db.get(Workspace, ws).kind != "judge":
            raise HTTPException(403, "Start the judge walkthrough first")
        prep = judge_sponsor.from_db(db, ws)
        contexts = [
            {
                "id": r.id,
                "slug": r.slug,
                "status": r.status,
                "probe": r.last_probe_hit_ratio,
                "probed_at": r.last_probe_at,
            }
            for r in db.scalars(
                select(Context)
                .where(
                    Context.id.in_(prep.get("context_ids", [])),
                    Context.workspace_id == ws,
                )
                .order_by(Context.token_len)
            )
        ]
        hot = len(contexts) == 2 and all(
            r["status"] == "hot" and (r["probe"] or 0) >= 0.95 for r in contexts
        )
        m = db.scalar(
            select(Mandate)
            .where(Mandate.workspace_id == ws)
            .order_by(Mandate.created_at.desc())
        )
        if not m:
            return {
                "steps": [hot, False, False, False, False, False],
                "contexts": contexts,
                "preparation": prep,
            }
        marker = db.get(Control, "judge-main-run:" + m.id)
        original = db.get(AgentRun, marker.data["run_id"]) if marker else None
        parent = (
            db.get(Control, "judge-fault-parent:" + original.id) if original else None
        )
        follow = db.get(AgentRun, parent.data["run_id"]) if parent else None
        orig_ids = (
            set(db.scalars(select(Call.id).where(Call.run_id == original.id)))
            if original
            else set()
        )
        targets = (
            set(
                db.scalars(
                    select(Call.id).where(
                        Call.run_id == follow.id,
                        Call.sla_breach.is_(True),
                        Call.public_charge_micros > 0,
                        Call.fault_injected_ms >= 1200,
                    )
                )
            )
            if follow
            else set()
        )
        caps = list(
            db.scalars(
                select(Capture)
                .where(
                    Capture.mandate_id == m.id,
                    Capture.workspace_id == ws,
                    Capture.status.in_(ledger.CAPTURED),
                )
                .order_by(Capture.created_at)
            )
        )
        cap_ids = {c.id for c in caps}
        refunds = [
            r
            for r in db.scalars(
                select(Refund).where(
                    Refund.workspace_id == ws,
                    Refund.capture_id.in_(cap_ids),
                    Refund.reason == "sla",
                    Refund.status == "COMPLETED",
                )
            )
            if targets.intersection(r.call_ids_json)
        ]
        initial = [
            {
                "id": c.paypal_capture_id,
                "amount_cents": c.amount_cents,
                "fee_cents": c.fee_cents,
                "net_cents": c.amount_cents - c.fee_cents,
            }
            for c in caps
            if orig_ids.intersection(c.call_ids_json)
        ]
        linked = (
            set().union(*(set(r.call_ids_json) for r in refunds)) if refunds else set()
        )
        done = close_view(db, ws, m.id)
        flags = [
            hot or bool(original),
            bool(m.authorization_id),
            bool(original and original.status == "completed" and len(orig_ids) == 10),
            bool(initial),
            bool(
                follow
                and follow.status == "completed"
                and len(targets) == 3
                and targets <= linked
            ),
            bool(
                done.get("status") == "SUCCESS"
                and len(done.get("items", [])) == 2
                and all(i["status"] == "SUCCESS" for i in done["items"])
                and (done.get("reconciliation") or {}).get("ok")
            ),
        ]
        return {
            "steps": flags,
            "contexts": contexts,
            "preparation": prep,
            "mandate": service.view(m),
            "run": run_view(original),
            "followup": run_view(follow),
            "captures": initial,
            "refunds": [
                {
                    "id": r.paypal_refund_id,
                    "amount_cents": r.amount_cents,
                    "call_ids": list(r.call_ids_json),
                }
                for r in refunds
            ],
            "fault_calls": list(sorted(targets)),
            "settlement": done,
        }


@router.post("/task")
def start_guided_task(body: GuideTask, bg: BackgroundTasks, req: Request, ws=wsdep):
    from backend.app.agents.tasks import TASKS

    if body.task_id not in TASKS:
        raise HTTPException(422, "Choose a built-in coding task")
    with session_scope(write=False) as db:
        if db.get(Workspace, ws).kind != "judge":
            raise HTTPException(403, "A judge workspace is required")
        own = owned(db, Mandate, body.mandate_id, ws)
        job = db.get(Control, judge_sponsor.JOB + ws)
        if not job:
            raise HTTPException(409, "Start the guided workspace first")
        ctx = db.scalar(
            select(Context).where(
                Context.workspace_id == ws,
                Context.id.in_(job.data["contexts"]),
                Context.slug == TASKS[body.task_id][0],
            )
        )
        if not ctx:
            raise HTTPException(409, "The guided source is missing")
        body_ = Start(
            mandate_id=own.id,
            context_id=ctx.id,
            task_id=body.task_id,
            cold_fraction=0.25,
            est_turns=10,
        )
    return start_run(body_, bg, req, ws, guided_main=True)


@router.post("/finish")
def finish(body: ContinueWith, ws=wsdep):
    mid = body.mandate_id
    with service.lock(ws):
        tree = walkthrough(ws)
        if (tree.get("mandate") or {}).get("id") != mid or not tree["steps"][4]:
            raise HTTPException(
                409, "Confirm all three fault refunds on this mandate first"
            )
        if tree["steps"][5]:
            return tree
        closed = service.close(ws, mid)
        if tree["preparation"].get("status") != "ready":
            return {**tree, "mandate": closed, "waiting_for_sponsor": True}
        with session_scope() as db:
            marker = db.get(Control, "judge-finish:" + mid)
            if not marker:
                marker = Control(
                    key="judge-finish:" + mid,
                    data={
                        "status": "starting",
                        "started": datetime.now(timezone.utc).isoformat(),
                    },
                )
                db.add(marker)
                db.flush()
            bid = marker.data.get("batch_id")
            if not bid:
                # Recover a locally committed, provider-idempotent payout after a lost response.
                prior = db.scalar(
                    select(PayoutBatch)
                    .where(
                        PayoutBatch.workspace_id == ws,
                        PayoutBatch.created_at
                        >= datetime.fromisoformat(marker.data["started"]),
                    )
                    .order_by(PayoutBatch.created_at.asc())
                )
                bid = prior.id if prior else None
        batch = settle(ws, bid)
        if not batch.get("id"):
            raise HTTPException(
                409, "The two publishers do not yet have a funded payout"
            )
        with session_scope() as db:
            marker = db.get(Control, "judge-finish:" + mid)
            marker.data = {
                **marker.data,
                "batch_id": batch["id"],
                "status": batch["status"],
            }
            data = close_view(db, ws, mid)
        if (
            data["status"] == "SUCCESS"
            and len(data["items"]) == 2
            and all(i["status"] == "SUCCESS" for i in data["items"])
        ):
            result = reconcile(ws)
            if not result["ok"] or not result["checked"] or result["differences"]:
                raise HTTPException(
                    409, "The PayPal object reconciliation has outstanding differences"
                )
            with session_scope() as db:
                marker = db.get(Control, "judge-finish:" + mid)
                marker.data = {
                    **marker.data,
                    "reconciliation": {
                        k: result[k]
                        for k in ("ok", "checked", "clearing_micros", "differences")
                    },
                }
        return walkthrough(ws)
