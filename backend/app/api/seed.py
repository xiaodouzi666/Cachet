"""Track only operator-declared, completed live sessions in the test dataset."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from backend.app.api.access import admin, current_workspace, owned
from backend.app.db import session_scope
from backend.app.models import AgentRun, Call, Capture, Mandate, Workspace
from backend.app.operations import Control

router = APIRouter(prefix="/operator", dependencies=[Depends(admin)])
wsdep = Depends(current_workspace)


def seed_state(db, ws):
    prefix = "operator-seed-run:" + ws + ":"
    keys = db.scalars(
        select(Control.key).where(Control.key.startswith(prefix)).order_by(Control.key)
    )
    ids = [k[len(prefix) :] for k in keys]
    return {
        "operator_seeded": bool(ids),
        "operator_seed_run_count": len(ids),
        "operator_seed_run_ids": ids,
    }


@router.post("/seed-mandates/{identifier}")
def reserve_seed(identifier: str, ws=wsdep):
    """Declare intent before this operator starts any runs against the dedicated mandate."""
    with session_scope() as db:
        mandate = owned(db, Mandate, identifier, ws)
        if db.get(Workspace, ws).kind != "judge":
            raise HTTPException(
                403, "Seed traffic requires a dedicated judge workspace"
            )
        key = "operator-seed-mandate:" + mandate.id
        marker = db.get(Control, key)
        if marker:
            return {"reserved_mandate_id": mandate.id}
        if (
            mandate.status != "ACTIVE"
            or not mandate.authorization_id
            or not mandate.paypal_order_id
        ):
            raise HTTPException(
                409, "Seed declaration requires an active sandbox authorization"
            )
        if db.scalar(select(AgentRun.id).where(AgentRun.mandate_id == mandate.id)):
            raise HTTPException(
                409, "Declare the seed mandate before starting its first run"
            )
        db.add(Control(key=key, data={"workspace_id": ws}))
        return {"reserved_mandate_id": mandate.id}


@router.post("/seed-runs/{identifier}")
def confirm_seed(identifier: str, ws=wsdep):
    with session_scope() as db:
        run = owned(db, AgentRun, identifier, ws)
        mandate = owned(db, Mandate, run.mandate_id, ws)
        key = "operator-seed-run:" + ws + ":" + run.id
        intent = db.get(Control, "operator-seed-mandate:" + mandate.id)
        if not intent or intent.data["workspace_id"] != ws:
            raise HTTPException(
                409, "The operator must reserve the mandate before the run"
            )
        if (
            run.status != "completed"
            or run.mode != "live"
            or not run.ended_at
            or mandate.status != "CLOSED"
            or not mandate.authorization_id
        ):
            raise HTTPException(
                409,
                "Only a completed live run with a closed authorization can enter the dataset",
            )
        if db.scalar(
            select(AgentRun.id).where(
                AgentRun.mandate_id == mandate.id, AgentRun.id != run.id
            )
        ):
            raise HTTPException(409, "Seed sessions require one run per mandate")
        calls = list(db.scalars(select(Call).where(Call.run_id == run.id)))
        if len(calls) < 10 or any(
            c.mode != "live" or not c.input_hash or c.output_tokens <= 0 for c in calls
        ):
            raise HTTPException(409, "At least ten committed live turns are required")
        captured = {
            cid
            for c in db.scalars(
                select(Capture).where(
                    Capture.mandate_id == mandate.id,
                    Capture.paypal_capture_id.is_not(None),
                    Capture.amount_cents > 0,
                    Capture.status.in_(["COMPLETED", "REFUNDED", "PARTIALLY_REFUNDED"]),
                )
            )
            for cid in c.call_ids_json
        }
        if not captured.intersection(c.id for c in calls):
            raise HTTPException(
                409, "No confirmed sandbox capture is linked to this run"
            )
        if db.get(Control, key) is None:
            db.add(Control(key=key, data={"mandate_id": mandate.id}))
        return {"operator_seed_run_id": run.id, "operator_seeded": True}
