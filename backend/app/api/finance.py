import secrets
from decimal import Decimal
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from backend.app import marketplace
from backend.app.api.access import (
    gateway,
    client_ip,
    current_workspace,
    digest,
    limit,
    owned,
)
from backend.app.api.catalog import flush_and_schedule_rewarm
from backend.app.api import seed
from backend.app.config import value
from backend.app.db import session_scope
from backend.app.models import (
    AgentRun,
    Call,
    Capture,
    Consumer,
    Context,
    LedgerEntry,
    Mandate,
    PayoutBatch,
    PayoutItem,
    Refund,
    Workspace,
    WebhookEvent,
)
from backend.app.operations import Control, RunEvent
from backend.app.money import usd_to_micros
from backend.app.gateway.modes import health
from backend.app.paypal.reconciliation import reconcile
from backend.app.settlement import service
from backend.app.settlement.audit import audit
from backend.app.settlement.payout_job import settle

router = APIRouter(prefix="/api", dependencies=[Depends(gateway)])
router.include_router(seed.router)
wsdep = Depends(current_workspace)


class WorkspaceInput(BaseModel):
    kind: str = Field(default="judge", pattern="^(judge|user)$")
    switch: bool = False


@router.post("/judge/workspace")
def start_workspace(
    request: Request,
    response: Response,
    body: WorkspaceInput | None = None,
    cachet_workspace: str | None = Cookie(None),
    cachet_user_workspace: str | None = Cookie(None),
):
    with session_scope() as db:
        requested = body.kind if body else "judge"
        options = dict(
            httponly=True,
            secure=value("PUBLIC_WEB_URL").startswith("https://"),
            samesite="lax",
            max_age=86400 * 45,
        )
        if cachet_workspace:
            ws = db.scalar(
                select(Workspace).where(Workspace.cookie_id == digest(cachet_workspace))
            )
            if ws and (
                ws.kind == requested
                or (ws.kind == "judge" and not (body and body.switch))
            ):
                return {"workspace_id": ws.id, "kind": ws.kind}
            if ws and ws.kind == "user":
                response.set_cookie(
                    "cachet_user_workspace",
                    cachet_workspace,
                    path="/api/judge/workspace",
                    **options,
                )
        if requested == "user" and cachet_user_workspace:
            prior = db.scalar(
                select(Workspace).where(
                    Workspace.cookie_id == digest(cachet_user_workspace),
                    Workspace.kind == "user",
                )
            )
            if prior:
                response.set_cookie(
                    "cachet_workspace", cachet_user_workspace, path="/", **options
                )
                return {"workspace_id": prior.id, "kind": prior.kind}
        limit(db, ["workspace:" + client_ip(request)], 20, 3600)
        secret = secrets.token_urlsafe(32)
        ws = Workspace(cookie_id=digest(secret), kind=body.kind if body else "judge")
        db.add(ws)
        db.flush()
        while True:
            salt = secrets.token_hex(4)
            if not db.scalar(select(Consumer.id).where(Consumer.salt_hex == salt)):
                break
        db.add(Consumer(workspace_id=ws.id, salt_hex=salt, name="Workspace agent"))
        response.set_cookie(
            "cachet_workspace",
            secret,
            httponly=True,
            secure=value("PUBLIC_WEB_URL").startswith("https://"),
            samesite="lax",
            max_age=86400 * 45,
            path="/",
        )
        return {"workspace_id": ws.id, "kind": ws.kind}


def fields(obj, names):
    return {name: getattr(obj, name) for name in names.split()}


def saved_application_check(db, run):
    done = (
        db.scalar(
            select(RunEvent.payload)
            .where(RunEvent.run_id == run.id, RunEvent.event_type == "done")
            .order_by(RunEvent.id.desc())
            .limit(1)
        )
        if run.status == "completed"
        else None
    )
    if done and "application_check" in done:
        return {
            k: done[k]
            for k in (
                "application_check",
                "verified_application_turn",
                "earlier_application_check",
            )
            if k in done
        }
    return {
        "application_check": {
            "status": "unverified",
            "reason": "This run has no saved HTTP verification.",
        },
        "verified_application_turn": None,
    }


class PrepareWalkthrough(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/judge/prepare")
def start_preparation(body: PrepareWalkthrough, bg: BackgroundTasks, ws=wsdep):
    from backend.app import judge_sponsor
    return judge_sponsor.prepare(ws,bg)


@router.get("/judge/preparation")
def preparation_status(ws=wsdep):
    from backend.app import judge_sponsor
    return judge_sponsor.status(ws)


@router.get("/judge/state")
def state(ws=wsdep):
    with session_scope(write=False) as db:
        kind = db.get(Workspace, ws).kind
        m = db.scalar(
            select(Mandate)
            .where(Mandate.workspace_id == ws)
            .order_by(Mandate.created_at.desc())
        )
        run = db.scalar(
            select(AgentRun)
            .where(AgentRun.workspace_id == ws)
            .order_by(AgentRun.created_at.desc())
        )
        sandbox = value("PAYPAL_ENV", "sandbox") == "sandbox"
        buyer = sandbox and kind == "judge"
        provenance = seed.seed_state(db, ws)
        registration = db.get(Control, "operator-soak-registration")
        soak_mandate = registration.data["mandate_id"] if registration and registration.data.get("workspace_id") == ws else None
        latest_run = (
            fields(run, "id mandate_id task_id status mode quote_json") if run else None
        )
        if latest_run:
            latest_run.update(saved_application_check(db, run))
            latest_run["operator_soak"] = bool(run.quote_json.get("operator_soak"))
            latest_run["operator_seeded"] = (
                run.id in provenance["operator_seed_run_ids"]
            )
        return {
            **provenance,
            "operator_soak_mandate_id": soak_mandate,
            "workspace_id": ws,
            "kind": kind,
            "mandate": service.view(m) if m else None,
            "run": latest_run,
            "sandbox_buyer_email": value("JUDGE_BUYER_EMAIL") if buyer else None,
            "sandbox_buyer_password": value("JUDGE_BUYER_PASSWORD") if buyer else None,
            "publisher_receivers": {
                s: value(f"PUBLISHER_{s.upper()}_EMAIL")
                for s in ("starlette", "fastapi")
            }
            if buyer
            else {},
            "next_public_paypal_client_id": value("NEXT_PUBLIC_PAYPAL_CLIENT_ID")
            or value("PAYPAL_CLIENT_ID"),
            "card_enabled": bool(
                sandbox
                and value("PAYPAL_CLIENT_SECRET")
                and value("SANDBOX_TEST_CARD_NUMBER")
                and value("SANDBOX_TEST_CARD_EXPIRY")
            ),
        }


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cap_usd: Decimal = Field(
        default_factory=lambda: Decimal(value("MANDATE_DEFAULT_CAP_USD", "5")),
        gt=0,
        le=20,
        decimal_places=2,
    )


@router.post("/mandates")
def create(body: Budget, ws=wsdep):
    cap = usd_to_micros(body.cap_usd)
    if cap > usd_to_micros(value("MANDATE_MAX_CAP_USD", "20")):
        raise HTTPException(422, "Mandate exceeds the configured cap")
    return service.open_order(ws, cap)


@router.post("/mandates/card")
def card(body: Budget, ws=wsdep):
    cap = usd_to_micros(body.cap_usd)
    if cap > usd_to_micros(value("MANDATE_MAX_CAP_USD", "20")):
        raise HTTPException(422, "Mandate exceeds the configured cap")
    if not value("SANDBOX_TEST_CARD_NUMBER") or not value("SANDBOX_TEST_CARD_EXPIRY"):
        raise HTTPException(503, "Sandbox card authorization is not configured")
    m = service.open_order(ws, cap, card=True)
    return service.authorize(ws, m["id"])


@router.post("/mandates/{identifier}/authorize")
def authorize(identifier: str, ws=wsdep):
    return service.authorize(ws, identifier)


@router.get("/mandates/{identifier}")
def get_mandate(identifier: str, ws=wsdep):
    with session_scope(write=False) as db:
        return service.view(owned(db, Mandate, identifier, ws))


@router.post("/mandates/{identifier}/close")
def close(identifier: str, ws=wsdep):
    return service.close(ws, identifier)


class MandateAction(BaseModel):
    mandate_id: str


class Fault(BaseModel):
    ttft_delay_ms: int = Field(ge=1, le=5000)
    calls: int = Field(ge=1, le=10)


def throttle(db, ws, key, count=15):
    if db.get(Workspace, ws).kind != "judge":
        raise HTTPException(403, "This control requires a judge workspace")
    limit(db, [key + ":" + ws], count, 60)


@router.post("/judge/fault")
def fault(body: Fault, ws=wsdep):
    with session_scope() as db:
        throttle(db, ws, "inject")
        db.merge(
            Control(
                key="fault:" + ws,
                data={"ms": body.ttft_delay_ms, "remaining": body.calls},
            )
        )
        return {
            "test_operation": "Fault injection (test)",
            "ttft_delay_ms": body.ttft_delay_ms,
            "calls": body.calls,
        }


@router.post("/judge/flush-cache")
def flush(ws=wsdep):
    if health()["mode"] != "live":
        raise HTTPException(
            409, "The judge flush control requires the primary live GPU"
        )
    with session_scope() as db:
        throttle(db, ws, "flush")
        limit(db, ["global-cache-flush"], 1, 600)
    return flush_and_schedule_rewarm(ws)


@router.get("/judge/capture-preview")
def capture_preview(mandate_id: str, ws=wsdep):
    with session_scope(write=False) as db:
        if db.get(Workspace, ws).kind != "judge":
            raise HTTPException(403, "This control requires a judge workspace")
        return service.capture_preview(owned(db, Mandate, mandate_id, ws))


@router.post("/judge/capture-now")
def capture_now(body: MandateAction, ws=wsdep):
    with session_scope() as db:
        throttle(db, ws, "capture")
    return service.capture(ws, body.mandate_id, force=True) or {
        "status": "below_one_cent"
    }


class FaultCaptureRequest(MandateAction):
    run_id: str = Field(min_length=1, max_length=64)


@router.post("/judge/fault-capture")
def collect_fault_turns(body: FaultCaptureRequest, ws=wsdep):
    with session_scope() as db:
        throttle(db, ws, "capture")
    return service.capture_fault_run(ws, body.mandate_id, body.run_id)


@router.post("/judge/audit")
def audit_now(body: MandateAction, ws=wsdep):
    with session_scope() as db:
        throttle(db, ws, "audit")
    return audit(ws, body.mandate_id)


class SettlementRequest(BaseModel):
    batch_id: str | None = Field(default=None, min_length=1, max_length=64)


@router.post("/judge/settle")
def settle_now(body: SettlementRequest | None = None, ws=wsdep):
    with session_scope() as db:
        throttle(db, ws, "settle")
    return settle(ws, body.batch_id if body else None)


def listing(model, names, ws):
    with session_scope(write=False) as db:
        return [
            fields(r, names)
            for r in db.scalars(
                select(model)
                .where(model.workspace_id == ws)
                .order_by(model.created_at.desc())
            )
        ]


@router.get("/ledger")
def entries(ws=wsdep):
    return listing(
        LedgerEntry,
        "id txn_id account debit_micros credit_micros ref_id ref_type created_at",
        ws,
    )


@router.get("/captures")
def captures(ws=wsdep):
    rows = listing(
        Capture,
        "id mandate_id paypal_capture_id amount_cents final fee_cents status call_ids_json created_at",
        ws,
    )


    return [{**r, "net_cents": r["amount_cents"] - r["fee_cents"]} for r in rows]


@router.get("/refunds")
def refunds(ws=wsdep):
    rows = listing(
        Refund,
        "id capture_id paypal_refund_id amount_cents fee_reversal_cents reason call_ids_json status created_at",
        ws,
    )


    return [{**r, "fee_cents": r["fee_reversal_cents"], "net_cents": r["amount_cents"] - r["fee_reversal_cents"]}
            for r in rows]


@router.get("/payouts")
def payouts(ws=wsdep):
    batches = listing(
        PayoutBatch,
        "id sender_batch_id paypal_batch_id status fee_cents created_at",
        ws,
    )
    items = listing(
        PayoutItem,
        "id batch_id publisher_id paypal_item_id amount_cents fee_cents status",
        ws,
    )
    result = []
    for batch in batches:
        children = [item for item in items if item["batch_id"] == batch["id"]]
        for item in children:
            if item["status"] == "SUCCESS":
                item["net_cents"] = item["amount_cents"] + item["fee_cents"]
            else:
                item["fee_cents"] = item["net_cents"] = None
        amount = sum(item["amount_cents"] for item in children)
        fee = batch["fee_cents"] if children and all(item["status"] == "SUCCESS" for item in children) else None
        result.append({**batch, "items": children, "amount_cents": amount, "fee_cents": fee,
                       "net_cents": amount + fee if fee is not None else None})
    return result


@router.get("/royalties")
def royalties(ws=wsdep):
    with session_scope(write=False) as db:
        result = []
        for c in marketplace.settlement_contexts(db, ws):
            published = c.workspace_id == ws
            hits, gross, linear = marketplace.totals(
                db, c.id, buyer=None if published else ws
            )
            result.append(
                {
                    "id": c.id,
                    "parent_id": marketplace.parent_id(db, c),
                    "publisher_id": c.publisher_id,
                    "title": c.title,
                    "scope": "published" if published else "purchased",
                    "historical_totals_complete": marketplace.complete_history(db, c.id)
                    if published
                    else True,
                    "hit_tokens": hits,
                    "linear_micros": linear,
                    "shapley_micros": gross,
                }
            )
        return result


@router.get("/paypal-events")
def paypal_events(ws=wsdep):
    """Show receipts associated with this workspace's provider IDs, without raw payloads."""
    subscriptions = (
        (("CHECKOUT.ORDER.APPROVED",), Mandate.paypal_order_id),
        (("PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.AUTHORIZATION.VOIDED"), Mandate.authorization_id),
        (("PAYMENT.CAPTURE.COMPLETED",), Capture.paypal_capture_id),
        (("PAYMENT.CAPTURE.REFUNDED",), Refund.paypal_refund_id),
        (("PAYMENT.PAYOUTSBATCH.SUCCESS",), PayoutBatch.paypal_batch_id),
        (("PAYMENT.PAYOUTS-ITEM.SUCCEEDED",), PayoutItem.paypal_item_id),
    )
    owned_receipts = [
        and_(WebhookEvent.event_type.in_(types),
             WebhookEvent.resource_id.in_(select(column).where(column.class_.workspace_id == ws)))
        for types, column in subscriptions
    ]
    with session_scope(write=False) as db:
        return db.execute(select(
            WebhookEvent.event_type, WebhookEvent.resource_id,
            WebhookEvent.verified, WebhookEvent.received_at,
        ).where(WebhookEvent.verified.is_(True), or_(*owned_receipts)).order_by(
            WebhookEvent.received_at.desc(), WebhookEvent.id.desc(),
        ).limit(200)).mappings().all()


@router.get("/royalties/system")
def system_prefix(ws=wsdep):
    """P0 has no publisher. Its measured hits are scoped to this buyer's own calls."""
    with session_scope(write=False) as db:
        hits = db.scalar(select(func.coalesce(func.sum(Call.hits_json["system"].as_integer()), 0))
                         .where(Call.workspace_id == ws))
    return {"title": "P0 · System", "hit_tokens": hits, "scope": "workspace_calls"}


@router.get("/reconcile")
def reconciliation(ws=wsdep):
    return reconcile(ws)
