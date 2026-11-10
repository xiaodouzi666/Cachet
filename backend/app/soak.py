"""Bound operator-only live traffic to one recorded sandbox mandate and workspace."""

import secrets
from datetime import timedelta

from fastapi import Header, HTTPException
from sqlalchemy import func, select

from backend.app import ledger
from backend.app.api.access import owned
from backend.app.config import value
from backend.app.models import AgentRun, Call, Capture, CaptureAllocation, Context, Mandate, Refund, Workspace, utcnow
from backend.app.operations import Control

REGISTRATION = "operator-soak-registration"
RUN_BUDGET = 600
INITIAL_TARGET = 4_200_000


def operator_token(x_cachet_soak_token: str | None = Header(default=None)):
    configured = value("CACHET_SOAK_TOKEN")
    if len(configured) < 32 or configured == value("ADMIN_TOKEN"):
        raise HTTPException(503, "Configure a dedicated soak operator credential")
    if not x_cachet_soak_token or not secrets.compare_digest(configured, x_cachet_soak_token):
        raise HTTPException(403, "The dedicated soak operator credential is required")


def registered(db, ws, mid):
    marker = db.get(Control, REGISTRATION)
    if not marker or marker.data["workspace_id"] != ws or marker.data["mandate_id"] != mid:
        raise HTTPException(403, "This mandate is outside the operator soak registration")
    return marker


def claim(db, ws, context_id):
    marker = db.get(Control, REGISTRATION)
    if marker:
        if marker.data["workspace_id"] != ws or marker.data["context_id"] != context_id:
            raise HTTPException(409, "Another workspace already owns this recording slot")
        return marker
    ctx = owned(db, Context, context_id, ws)
    if (db.get(Workspace, ws).kind != "judge" or ctx.slug != "fastapi"
        or db.scalar(select(Mandate.id).where(Mandate.workspace_id == ws).limit(1))
        or db.scalar(select(AgentRun.id).where(AgentRun.workspace_id == ws).limit(1))):
        raise HTTPException(409, "Claim a fresh judge workspace and its published FastAPI context")
    marker = Control(key=REGISTRATION, data={"workspace_id":ws, "context_id":context_id, "mandate_id":None})
    db.add(marker)
    return marker


def register(db, ws, mid, context_id):
    marker = db.get(Control, REGISTRATION)
    if not marker or marker.data["workspace_id"] != ws or context_id != marker.data["context_id"]:
        raise HTTPException(403, "Claim this workspace before authorizing a recording budget")
    if marker.data["mandate_id"]:
        return registered(db, ws, mid)
    mandate = owned(db, Mandate, mid, ws)
    if (value("PAYPAL_ENV", "sandbox") != "sandbox" or mandate.payment_method != "card"
        or mandate.cap_micros != 5_000_000 or mandate.status != "ACTIVE"
        or not mandate.authorization_id or not mandate.paypal_order_id
        or db.scalar(select(AgentRun.id).where(AgentRun.workspace_id == ws).limit(1))):
        raise HTTPException(409, "Register the dedicated active $5 sandbox card authorization before its first run")
    now = utcnow()
    marker.data = {"workspace_id":ws, "mandate_id":mid, "context_id":context_id,
        "started":now.timestamp(), "until":(now + timedelta(hours=3)).timestamp(), "sequence":0,
        "last_run_id":None, "target_micros":INITIAL_TARGET, "extensions":0}
    return marker


def state(db, ws, mid):
    registration = registered(db, ws, mid).data
    mandate = owned(db, Mandate, mid, ws)
    runs = select(AgentRun.id).where(AgentRun.mandate_id == mid, AgentRun.quote_json["operator_soak"].as_boolean().is_(True))
    fault_ids = set(db.scalars(select(Call.id).where(Call.run_id.in_(runs), Call.fault_injected_ms > 0)))
    caps = list(db.scalars(select(Capture).where(Capture.mandate_id == mid)))
    paid_faults = set(db.scalars(select(CaptureAllocation.call_id).where(
        CaptureAllocation.capture_id.in_([c.id for c in caps]), CaptureAllocation.call_id.in_(fault_ids),
        CaptureAllocation.public_micros > 0))) if fault_ids else set()
    refunds = list(db.scalars(select(Refund).where(Refund.capture_id.in_([c.id for c in caps]), Refund.status == "COMPLETED")))
    captured_fault_refunds = [x.paypal_refund_id for x in refunds if x.paypal_refund_id and paid_faults.intersection(x.call_ids_json)]
    last = db.get(AgentRun, registration["last_run_id"]) if registration["last_run_id"] else None
    balances, available = {}, {}
    for layer in db.scalars(select(Context).where(Context.workspace_id == ws, Context.slug.in_(["starlette", "fastapi"]))):
        balances[layer.slug] = -ledger.balance(db, ws, ledger.payable(layer.publisher_id))
        available[layer.slug] = ledger.available_payable(db, ws, layer.publisher_id)
    return {**registration, "operator_test_traffic":True, "publisher_payable_micros":balances,
        "publisher_available_micros":available,
        "publisher_pending_or_reserved_micros":{slug:max(0, book) - available[slug] for slug, book in balances.items()},
        "publisher_offset_micros":{slug:max(0, -book) for slug, book in balances.items()}, "status":mandate.status, "last_run_status":last.status if last else None,
        "authorization_id":mandate.authorization_id, "paypal_order_id":mandate.paypal_order_id,
        "captured_micros":mandate.captured_micros, "pending_micros":mandate.accrued_uncaptured_micros,
        "billable_micros":mandate.captured_micros + mandate.accrued_uncaptured_micros,
        "metered_turns":db.scalar(select(func.count(Call.id)).where(Call.run_id.in_(runs))),
        "fault_turns":len(fault_ids), "captured_fault_turns":len(paid_faults),
        "fault_refund_ids":captured_fault_refunds, "refund_ids":[r.paypal_refund_id for r in refunds],
        "capture_ids":[c.paypal_capture_id for c in caps], "threshold_captures":sum(c.amount_cents >= 200 and not c.final for c in caps)}


def admit(db, ws, mid, sequence):
    marker = registered(db, ws, mid)
    snap = state(db, ws, mid)
    if sequence == snap["sequence"] and snap["last_run_id"]:
        return marker, {"run_id":snap["last_run_id"], "status":snap["last_run_status"]}
    if sequence != snap["sequence"] + 1 or sequence > RUN_BUDGET:
        raise HTTPException(409, "The soak sequence is stale or exhausted")
    if utcnow().timestamp() >= snap["until"] or snap["billable_micros"] >= snap["target_micros"]:
        raise HTTPException(409, "The soak has reached its time or spending target")
    if snap["status"] != "ACTIVE" or snap["last_run_status"] in {"queued", "running"}:
        raise HTTPException(409, "The previous run must finish on this active mandate")
    return marker, None


def next_delay(db, ws, run):
    """Arm the six marked live delays within one cent of the automatic capture threshold."""
    marker = registered(db, ws, run.mandate_id)
    if marker.data["last_run_id"] != run.id:
        return 0
    prior = db.scalar(select(func.count(Call.id)).join(AgentRun, Call.run_id == AgentRun.id).where(
        AgentRun.mandate_id == run.mandate_id, AgentRun.quote_json["operator_soak"].as_boolean().is_(True),
        Call.fault_injected_ms > 0))
    if prior >= 6:
        return 0
    mandate = db.get(Mandate, run.mandate_id)
    if not marker.data.get("fault_armed") and mandate.accrued_uncaptured_micros < 1_990_000:
        return 0
    if not marker.data.get("fault_armed"):
        marker.data = {**marker.data, "fault_armed":True}
    return 1200


def recording_targets_met(snapshot):
    eligible = snapshot["publisher_available_micros"]
    return (snapshot["threshold_captures"] >= 2 and snapshot["fault_turns"] == 6
            and bool(snapshot["fault_refund_ids"]) and len(eligible) == 2 and min(eligible.values()) >= 250_000)


def extend(db, ws, mid, window):
    marker = registered(db, ws, mid)
    snap = state(db, ws, mid)
    if window == snap["extensions"]:
        return snap
    if window != snap["extensions"] + 1 or snap["status"] != "ACTIVE" or snap["last_run_status"] in {"queued", "running"}:
        raise HTTPException(409, "The requested continuation window is unavailable")
    if utcnow().timestamp() < snap["until"] and snap["billable_micros"] < snap["target_micros"]:
        raise HTTPException(409, "The current measurement window is still open")
    if snap["billable_micros"] >= 4_750_000:
        raise HTTPException(409, "The remaining budget is reserved for the recording")
    if recording_targets_met(snap):
        raise HTTPException(409, "The recording targets are already satisfied")
    marker.data = {**marker.data, "extensions":window, "until":(utcnow() + timedelta(hours=3)).timestamp(),
                   "target_micros":4_750_000}
    return state(db, ws, mid)
