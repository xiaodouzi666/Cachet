"""Run the coding agent and persist each metered turn before publishing its SSE event."""

import json
import threading
import time
from decimal import Decimal, ROUND_CEILING
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from backend.app import ledger, marketplace
from backend.app.agents.anomaly import payer_key, same_payer_layers
from backend.app.agents.inference import live_turns
from backend.app.agents.quote_reason import generate_reason, recorded_reason
from backend.app.agents.tasks import TASKS, STEPS
from backend.app.api.access import admin, client_ip, current_workspace, limit, owned
from backend.app.api.catalog import chain, observed_live_call, pending_flush_id, warm_after_miss
from backend.app.config import band_for, calibration, value
from backend.app.db import session_scope
from backend.app.gateway.billing import Span, Rates, price_call, sla_breached
from backend.app.gateway.hosted import checked_profile, fingerprint
from backend.app.gateway.modes import health
from backend.app.gateway.prompt_builder import Prefix
from backend.app.gateway.quote import quote
from backend.app.gateway.sglang_client import Engine
from backend.app.models import (
    AgentRun,
    Call,
    Consumer,
    Mandate,
    Publisher,
    utcnow,
)
from backend.app.operations import Control, RunEvent
from backend.app.replay.recordings import application_evidence, load
from backend.app.settlement.service import capture, ensure_payer_link, lock

router = APIRouter(prefix="/api", dependencies=[Depends(admin)])
wsdep = Depends(current_workspace)
slots = threading.BoundedSemaphore(2)


class Start(BaseModel):
    mandate_id: str
    context_id: str
    task_id: str = Field(max_length=64)
    task_text: str | None = Field(default=None, min_length=3, max_length=1500)
    cold_fraction: Decimal = Field(
        default_factory=lambda: Decimal(value("COLD_FRACTION_DEFAULT", ".25")),
        ge=0,
        le=1,
    )
    est_turns: int = Field(default=10, ge=1, le=30)


def emit(db, run_id, kind, payload):
    db.add(RunEvent(run_id=run_id, event_type=kind, payload=payload))


def public_explanation(receipt):
    # The full token sequence and inference counters stay in the GPU evidence.
    fields = ("source", "reason", "model", "generated_at", "input_hash", "answer_sha256")
    return {field: receipt[field] for field in fields}


def replay_turns(track, owners, fault_source):
    for item in track["turns"]:
        r = dict(item)
        spans = [
            dict(s, owner=owners[s["owner"]]) if s["kind"] == "publisher" else dict(s)
            for s in r["spans"]
        ]
        yield dict(
            kind="estimate",
            turn=r["turn"],
            prompt_tokens=r["prompt_tokens"],
            spans=spans,
            output_limit=r["output_tokens"],
        )
        delay = fault_source()
        started = time.perf_counter()
        time.sleep(r["ttft_ms"] / 1000)
        before_fault = time.perf_counter()
        if delay:
            time.sleep(delay / 1000)
        first = time.perf_counter()
        time.sleep(max(0, r["total_ms"] - r["ttft_ms"]) / 1000)
        yield dict(
            r,
            kind="result",
            spans=spans,
            mode="replay",
            ttft_ms=(first - started) * 1000,
            total_ms=(time.perf_counter() - started) * 1000,
            engine_ttft_ms=r["ttft_ms"],
            fault_injected_ms=(first - before_fault) * 1000 if delay else 0,
        )


def next_fault(ws):
    with session_scope() as db:
        control = db.get(Control, "fault:" + ws)
        if control and control.data["remaining"] > 0:
            control.data = {**control.data, "remaining": control.data["remaining"] - 1}
            return control.data["ms"]
        return 0


def reserve(ws, mandate_id, estimate, rates):
    # The public prefix is free on misses; all public tokens hitting is its worst case.
    total = Decimal(rates.output) * estimate["output_limit"]
    for s in estimate["spans"]:
        worst = (
            max(Decimal(rates.full), Decimal(rates.private))
            if s["kind"] == "private"
            else Decimal(rates.shared)
        )
        total += worst * (s["end"] - s["start"])
    with session_scope(write=False) as db:
        m = owned(db, Mandate, mandate_id, ws)
        if (
            m.status != "ACTIVE"
            or m.cap_micros - m.captured_micros - m.accrued_uncaptured_micros
            < int(total.to_integral_value(rounding=ROUND_CEILING))
        ):
            raise HTTPException(
                409, "The remaining authorized budget cannot cover the next turn"
            )


def ensure_budget_before_next(iterator, ws, mid, rates):
    for item in iterator:
        if item["kind"] == "estimate":
            reserve(ws, mid, item, rates)
        else:
            yield item


def persist(ws, run_id, mid, owners, meter, profile, rates):
    spans = []
    with session_scope() as db:
        run = owned(db, AgentRun, run_id, ws)
        if run.status != "running":
            raise HTTPException(409, "Agent run has ended")
        m = owned(db, Mandate, mid, ws)
        pubs = {name: db.get(Publisher, pid) for name, pid in owners.items()}
        identity = db.get(Control, payer_key(m))
        if identity is None:
            raise ValueError("Missing verified payer identity for the mandate")
        self_layers = same_payer_layers(identity.data, pubs)
        # Preserve separate layer identities when two layers have the same publisher.
        ctxlist = run.quote_json["chain"]
        index = 0
        for s in meter["spans"]:
            entry = dict(s)
            if s["kind"] == "publisher":
                entry["slug"] = ctxlist[index]
                index += 1
            spans.append(entry)
        parts = [
            Span(s["kind"], s.get("slug", s["owner"]), s["start"], s["end"])
            for s in spans
        ]
        args = dict(
            spans=parts,
            cached_tokens=meter["cached_tokens"],
            output_tokens=meter["output_tokens"],
            rates=rates,
            alpha=profile["fit"]["alpha"],
            beta=profile["fit"]["beta"],
            self_publishers=self_layers,
        )
        actual = price_call(**args)
        linear = price_call(**args, method="linear")

        def merged(result):
            shares = {}
            for slug, micros in result.royalty_micros.items():
                pid = owners[slug]
                shares[pid] = shares.get(pid, 0) + micros
            return shares

        public_hits = 0
        for s in spans:
            s["hits"] = max(0, min(meter["cached_tokens"], s["end"]) - s["start"])
            if s["kind"] != "private":
                public_hits += s["hits"]
            if s.get("slug"):
                s["self_royalty_suppressed"] = (
                    s["slug"] in self_layers and s["hits"] > 0
                )
                s["royalty_micros"] = actual.royalty_micros[s["slug"]]
                s["linear_micros"] = linear.royalty_micros[s["slug"]]
        delay = meter["fault_injected_ms"]
        ttft = meter["ttft_ms"]
        c = Call(
            workspace_id=ws,
            run_id=run_id,
            turn=meter["turn"],
            prompt_tokens=meter["prompt_tokens"],
            cached_tokens=meter["cached_tokens"],
            spans_json=spans,
            hits_json=actual.hits,
            output_tokens=meter["output_tokens"],
            input_hash=meter["input_hash"],
            output_text=meter["text"],
            ttft_ms=ttft,
            total_ms=meter["total_ms"],
            public_charge_micros=actual.public_charge_micros,
            charge_micros=actual.charge_micros,
            royalty_json=merged(actual),
            shapley_royalty_json=merged(actual),
            linear_royalty_json=merged(linear),
            platform_micros=actual.platform_micros,
            sla_breach=sla_breached(
                public_hits,
                ttft,
                band_for(meter["prompt_tokens"], profile)["warm_p50_ms"],
                float(value("SLA_FACTOR", "2")),
            ),
            fault_injected_ms=delay,
            mode=meter["mode"],
        )
        db.add(c)
        db.flush()
        ledger.accrue_call(db, c)
        marketplace.record_usage(db, c, run.quote_json.get("context_ids"))
        m.last_activity_at = utcnow()
        payload = {
            k: getattr(c, k)
            for k in (
                "id",
                "turn",
                "prompt_tokens",
                "cached_tokens",
                "spans_json",
                "hits_json",
                "output_tokens",
                "output_text",
                "ttft_ms",
                "total_ms",
                "charge_micros",
                "public_charge_micros",
                "royalty_json",
                "sla_breach",
                "fault_injected_ms",
                "mode",
            )
        }
        if meter.get("cache_flush_id"):
            payload["cache_flush_id"] = meter["cache_flush_id"]
        payload["measured_ttft_ms"] = meter["engine_ttft_ms"]
        emit(db, run_id, "turn", payload)
    return c


def execute(ws, identifier, context_id, tenant):
    try:
        with slots:
            with session_scope() as db:
                r = owned(db, AgentRun, identifier, ws)
                ctx = marketplace.visible_context(db, context_id, ws)
                layers = chain(db, ctx)
                source_chain = [
                    (c.slug, c.token_len, c.token_hash, c.version, c.tokenizer)
                    for c in layers
                ]
                owners = {c.slug: c.publisher_id for c in layers}
                r.status = "running"
                r.started_at = utcnow()
                config = r.quote_json
                mid = r.mandate_id
                task = r.task_text
                task_id = r.task_id
                mode = r.mode
                emit(db, r.id, "quote", config)
            ensure_payer_link(ws, mid)
            buy = config["decision"] == "buy"
            rates = Rates.from_settings(
                {
                    k: value(k)
                    for k in (
                        "PRICE_FULL",
                        "PRICE_SELF",
                        "PRICE_SHARED",
                        "ROYALTY_RATE",
                        "PRICE_OUT",
                    )
                }
            )
            checked_track = None
            engine = Engine(mode) if mode in {"live", "hosted"} else None
            try:
                profile = checked_profile(engine) if mode == "hosted" else calibration()
                if (
                    mode == "hosted"
                    and fingerprint(profile) != config["calibration_id"]
                ):
                    raise ValueError(
                        "Hosted calibration changed after this run was quoted."
                    )
                if engine is not None:
                    explanation = generate_reason(engine, config)
                    prefix = Prefix(engine, ctx.slug)
                    turns = live_turns(
                        engine,
                        prefix,
                        owners,
                        tenant,
                        task,
                        buy,
                        builtin_task=task_id in TASKS,
                        fault_source=lambda: next_fault(ws),
                        pending_flush=(
                            (lambda: pending_flush_id(ws))
                            if mode == "live" and buy
                            else None
                        ),
                    )
                else:
                    checked_track = load(
                        task_id, buy, source_chain=source_chain, request_quote=config
                    )
                    explanation = recorded_reason(checked_track["quote"])
                    turns = replay_turns(checked_track, owners, lambda: next_fault(ws))
                with session_scope() as db:
                    emit(
                        db,
                        identifier,
                        "decision",
                        {"decision": config["decision"], **public_explanation(explanation)},
                    )
                for meter in ensure_budget_before_next(turns, ws, mid, rates):
                    if engine is not None:
                        meter["mode"] = mode
                    if mode == "live" and meter.get("cache_flush_id"):
                        observed_live_call(ws, meter["cache_flush_id"])
                    with lock(ws):
                        c = persist(
                            ws,
                            identifier,
                            mid,
                            owners,
                            meter,
                            profile,
                            rates,
                        )
                        captured = capture(ws, mid)
                        if captured:
                            with session_scope() as db:
                                emit(db, identifier, "capture", captured)
                    if (
                        engine
                        and buy
                        and not meter.get("cache_flush_id")
                        and c.cached_tokens
                        < sum(len(ids) for _, ids in prefix.layers) * 0.95
                    ):
                        warm_after_miss(ctx.id, ctx.workspace_id, mode)
            finally:
                if engine is not None:
                    engine.close()
            with session_scope() as db:
                r = owned(db, AgentRun, identifier, ws)
                if r.status != "running":
                    return
                r.status = "completed"
                r.ended_at = utcnow()
                emit(
                    db,
                    identifier,
                    "done",
                    {
                        "run_id": identifier,
                        "status": r.status,
                        **application_evidence(checked_track),
                    },
                )
    except Exception as exc:
        from backend.app.paypal.client import PayPalError

        msg = (
            exc.detail
            if isinstance(exc, HTTPException)
            else str(exc)
            if isinstance(exc, (PayPalError, ValueError))
            else "Run interrupted. Inspect the server log and retry after resolving the service error."
        )
        import logging

        logging.getLogger(__name__).exception("Run %s failed", identifier)
        with session_scope() as db:
            r = owned(db, AgentRun, identifier, ws)
            if r.status == "failed":
                return
            r.status = "failed"
            r.ended_at = utcnow()
            emit(db, identifier, "error", {"message": msg})


@router.get("/tasks")
def tasks():
    return [
        {"id": k, "title": v[1], "source_slug": v[0], "turns": len(STEPS)}
        for k, v in TASKS.items()
    ]


@router.post("/agent/runs")
def start(body: Start, bg: BackgroundTasks, request: Request, ws=wsdep):
    serving = health()
    mode = serving["mode"]
    profile = calibration(mode)
    if mode == "unavailable":
        raise HTTPException(503, "Inference is unavailable")
    with session_scope() as db:
        m = owned(db, Mandate, body.mandate_id, ws)
        ctx = marketplace.visible_context(db, body.context_id, ws)
        if m.status != "ACTIVE" or not m.authorization_id:
            raise HTTPException(409, "A sandbox-authorized mandate is required")
        if m.expires_at and m.expires_at <= utcnow():
            raise HTTPException(409, "Mandate expired")
        if db.scalar(
            select(AgentRun.id).where(
                AgentRun.mandate_id == m.id, AgentRun.status.in_(["queued", "running"])
            )
        ):
            raise HTTPException(409, "A run is already using this mandate")
        if body.task_text is not None:
            if mode not in {"live", "hosted"}:
                raise HTTPException(422, "Free-text tasks require live inference")
            task = body.task_text
            key = "custom"
        elif body.task_id in TASKS and TASKS[body.task_id][0] == ctx.slug:
            key = body.task_id
            task = TASKS[key][1]
        else:
            raise HTTPException(
                422, "Select a built-in task for this context or supply a custom task"
            )
        limit(db, ["runs:" + ws], 3, 3600)
        limit(
            db,
            ["runs-ip:" + client_ip(request)],
            int(value("RUNS_PER_IP_HOUR", "20")),
            3600,
        )
        layers = chain(db, ctx)
        q = quote(
            sum(c.token_len for c in layers), len(STEPS), body.cold_fraction, profile
        )
        if mode == "hosted":
            q["calibration_id"] = fingerprint(profile)
            if q["calibration_id"] != serving.get("calibration_id"):
                raise HTTPException(
                    503, "Hosted calibration changed. Request a fresh quote."
                )
        if mode == "replay":
            load(
                key,
                q["decision"] == "buy",
                source_chain=[
                    (c.slug, c.token_len, c.token_hash, c.version, c.tokenizer)
                    for c in layers
                ],
                request_quote=q,
            )
            recorded_reason(q)
        q["chain"] = [c.slug for c in layers]
        q["context_ids"] = [c.id for c in layers]
        r = AgentRun(
            workspace_id=ws,
            mandate_id=m.id,
            task_id=key,
            task_text=task,
            decision=q["decision"],
            mode=mode,
            quote_json=q,
        )
        db.add(r)
        db.flush()
        position = db.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.status.in_(["queued", "running"]))
        )
        emit(db, r.id, "queued", {"position": position, "mode": mode})
        bg.add_task(execute, ws, r.id, ctx.id, db.get(Consumer, m.consumer_id).salt_hex)
        return {"run_id": r.id, "status": r.status}


def frame(kind, data, identifier=None):
    return (
        (f"id: {identifier}\n" if identifier is not None else "")
        + f"event: {kind}\ndata: "
        + json.dumps(data, ensure_ascii=True)
        + "\n\n"
    )


@router.get("/agent/runs/{identifier}/events")
def events(identifier: str, ws=wsdep, last_event_id: int = Header(default=0)):
    with session_scope(write=False) as db:
        owned(db, AgentRun, identifier, ws)

    def stream():
        cursor = last_event_id
        idle = 0
        while True:
            with session_scope(write=False) as db:
                r = owned(db, AgentRun, identifier, ws)
                results = list(
                    db.scalars(
                        select(RunEvent)
                        .where(RunEvent.run_id == identifier, RunEvent.id > cursor)
                        .order_by(RunEvent.id)
                    )
                )
                terminal = r.status in ("completed", "failed")
            for rec in results:
                cursor = rec.id
                yield frame(rec.event_type, rec.payload, cursor)
            if terminal:
                return
            idle += 1
            if idle % 30 == 0:
                yield ": heartbeat\n\n"
            time.sleep(0.4)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/reference-turn")
def archived_reference_turn(task_id: str = "rate-limit", decision: str = "buy"):
    """An inspectable historical GPU measurement separate from all workspace activity."""
    if task_id not in TASKS or decision not in {"buy", "compute"}:
        raise HTTPException(422, "Select a recorded task and a buy or compute decision")
    track = load(task_id, decision == "buy")
    row = track["turns"][0]
    return {
        "task_id": track["task_id"],
        "decision": decision,
        "quote": track["quote"],
        "decision_explanation": public_explanation(recorded_reason(track["quote"])),
        "final_turn": len(track["turns"]),
        "final_output_text": track["turns"][-1]["text"],
        "earlier_verified_output_text": (
            track["turns"][track["verified_application_turn"] - 1]["text"]
            if track.get("earlier_application_check") is not None
            else None
        ),
        **application_evidence(track),
        "recorded_at": track["recorded_at"],
        "model": track["model"],
        "mode": "recorded",
        **{
            key: row[key]
            for key in ("turn", "prompt_tokens", "cached_tokens", "ttft_ms", "spans")
        },
    }
