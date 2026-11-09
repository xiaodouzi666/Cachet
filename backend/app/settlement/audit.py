from sqlalchemy import select
from backend.app import ledger
from backend.app.api.access import owned
from backend.app.db import session_scope
from backend.app.models import AgentRun, Call, Capture, Mandate
from backend.app.settlement.service import lock, refund_calls


def audit(ws, mandate_id, call_id=None):
    reason = "dispute" if call_id else "sla"
    with lock(ws):
        with session_scope() as db:
            owned(db, Mandate, mandate_id, ws)
            query = (
                select(Call)
                .join(AgentRun, AgentRun.id == Call.run_id)
                .where(AgentRun.mandate_id == mandate_id)
            )
            calls = list(
                db.scalars(
                    query.where(Call.id == call_id)
                    if call_id
                    else query.where(Call.sla_breach.is_(True))
                )
            )
            if call_id and not calls:
                raise ValueError("Call was not found in this mandate")
            ids = {c.id for c in calls}
            corrections = sum(
                ledger.reverse_uncaptured(db, c, reason=reason) for c in calls
            )
            groups = [
                (cap.id, sorted(ids.intersection(cap.call_ids_json)))
                for cap in db.scalars(
                    select(Capture).where(Capture.mandate_id == mandate_id)
                )
            ]
        refunds = []
        for cid, subset in groups:
            if subset:
                receipt = refund_calls(ws, cid, subset, reason)
                if receipt:
                    refunds.append(receipt)
        return {
            "calls_reviewed": len(ids),
            "reversed_micros": corrections,
            "refunds": refunds,
        }
