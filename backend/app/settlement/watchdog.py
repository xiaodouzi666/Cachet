"""Recover committed work after restart and run bounded maintenance on durable workspaces."""

import logging
from datetime import timedelta
from threading import Event, Thread

from sqlalchemy import func, select

from backend.app.config import value
from backend.app.db import session_scope
from backend.app.models import AgentRun, Call, LedgerEntry, Mandate, Workspace, utcnow
from backend.app.operations import Control, RunEvent
from backend.app.paypal.reconciliation import reconcile
from backend.app.settlement.audit import audit
from backend.app.settlement.payout_job import refresh_submitted_payouts, settle
from backend.app.settlement.service import capture, close

LOG = logging.getLogger(__name__)


def recovery():
    with session_scope() as db:
        for run in db.scalars(
            select(AgentRun).where(AgentRun.status.in_(["queued", "running"]))
        ):
            run.status = "failed"
            run.ended_at = utcnow()
            db.add(
                RunEvent(
                    run_id=run.id,
                    event_type="error",
                    payload={
                        "message": "The API restarted. Previously committed calls remain in the ledger."
                    },
                )
            )


def end_idle_runs(now):
    cutoff = now - timedelta(minutes=int(value("RUN_IDLE_END_MIN", "10")))
    with session_scope() as db:
        for run in db.scalars(select(AgentRun).where(AgentRun.status == "running", AgentRun.started_at <= cutoff)):
            last_turn = db.scalar(select(func.max(Call.created_at)).where(Call.run_id == run.id))
            if (last_turn or run.started_at) <= cutoff:
                run.status, run.ended_at = "failed", now
                db.add(RunEvent(run_id=run.id, event_type="error", payload={"message": "Agent run ended after inactivity."}))


def maintain():
    from backend.app import judge_sponsor
    from backend.app.api.webhooks import retry_pending
    with session_scope(write=False) as db:
        pending = any(r.data["status"] not in judge_sponsor.TERMINAL
                      for r in db.scalars(select(Control).where(Control.key.startswith(judge_sponsor.JOB))))
    if pending:
        judge_sponsor.kick()

    try:
        retry_pending()
    except Exception:
        LOG.exception("Could not reproject pending verified webhooks")
    try:
        refresh_submitted_payouts()
    except Exception:
        LOG.exception("Could not read submitted PayPal payout batches")
    now = utcnow()
    end_idle_runs(now)
    with session_scope(write=False) as db:
        mandates = list(
            db.scalars(
                select(Mandate).where(
                    Mandate.status.in_(["ACTIVE", "CAPTURING", "CLOSING"])
                )
            )
        )
        workspaces = list(
            db.scalars(
                select(Workspace).where(
                    Workspace.id.in_(
                        select(Mandate.workspace_id).where(
                            Mandate.authorization_id.is_not(None)
                        )
                    )
                    | Workspace.id.in_(
                        select(LedgerEntry.workspace_id).where(
                            LedgerEntry.account.like("publisher:%:payable")
                        )
                    )
                )
            )
        )
        # Wait until a run terminates. Leave time for the judge's explicit audit.
        breached = list(
            db.execute(
                select(AgentRun.workspace_id, AgentRun.mandate_id, AgentRun.id)
                .join(Call, Call.run_id == AgentRun.id)
                .where(
                    Call.sla_breach.is_(True),
                    AgentRun.ended_at <= now - timedelta(minutes=2),
                )
                .distinct()
            )
        )
    for workspace_id, mandate_id, run_id in breached:
        key = "automatic-audit:" + run_id
        with session_scope(write=False) as db:
            if db.get(Control, key):
                continue
            # The guided button requests three delayed calls followed by capture and audit.
            # A disconnected browser must not let the automatic audit waive the calls first.
            run = db.get(AgentRun, run_id)
            guided = db.get(Control, "judge-fault-run:" + run_id)
            resume_capture = bool(guided and run.status == "completed" and db.get(Mandate, mandate_id).status == "ACTIVE")
            if resume_capture:
                measured = list(db.scalars(select(Call).where(Call.run_id == run_id)))
                resume_capture = len(measured) == 3 and all(
                    c.sla_breach and c.public_charge_micros > 0 and c.fault_injected_ms >= 1200 for c in measured
                )
        try:
            if resume_capture:
                from backend.app.settlement.service import capture_fault_run
                capture_fault_run(workspace_id, mandate_id, run_id)
            audit(workspace_id, mandate_id, run_id=run_id)
            with session_scope() as db:
                db.merge(Control(key=key, data={"at": now.timestamp()}))
        except Exception:
            LOG.exception("Could not audit mandate %s", mandate_id)
    for mandate in mandates:
        try:
            if mandate.status == "CAPTURING":
                capture(mandate.workspace_id, mandate.id)
            if (mandate.status == "CLOSING" or (mandate.expires_at and mandate.expires_at <= now)
                or mandate.captured_micros + mandate.accrued_uncaptured_micros >= mandate.cap_micros):
                close(mandate.workspace_id, mandate.id)
        except Exception:
            LOG.exception("Could not maintain mandate %s", mandate.id)
    for workspace in workspaces:
        for operation, interval in [(reconcile, 600), (settle, 86400)]:
            if operation is settle and workspace.kind == "judge":
                continue
            key = f"maintenance:{operation.__name__}:{workspace.id}"
            with session_scope(write=False) as db:
                previous = db.get(Control, key)
                if previous and now.timestamp() - previous.data["at"] < interval:
                    continue
            try:
                result = operation(workspace.id)
                with session_scope() as db:
                    db.merge(
                        Control(key=key, data={"at": now.timestamp(), "result": result})
                    )
            except Exception:
                LOG.exception("Maintenance failed for workspace %s", workspace.id)


def start():
    done = Event()

    def run():
        while not done.wait(30):
            try:
                maintain()
            except Exception:
                LOG.exception("Maintenance pass failed")

    Thread(target=run, name="cachet-maintenance", daemon=True).start()
    return done
