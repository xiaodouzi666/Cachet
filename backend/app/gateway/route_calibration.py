"""Measure and retain the SLA bands of the deployed API-to-GPU transport."""
from datetime import datetime, timezone
from statistics import median
from threading import Lock, Thread

from sqlalchemy import select

from backend.app.config import value
from backend.app.db import session_scope
from backend.app.gateway import registration
from backend.app.gateway.benchmark import benchmark
from backend.app.gateway.sglang_client import Engine
from backend.app.models import AgentRun
from backend.app.operations import Control

BANDS = (2000, 4000, 8000, 16000, 26000)
KEY = "inference:transport-calibration"
_worker = None
_lock = Lock()


def identity():
    gpu = registration.active() if registration.enabled() else None
    if gpu:
        return {**{k: gpu[k] for k in ("model", "gpu", "sglang_version")},
                "service": value("RENDER_SERVICE_ID", "local-api")}


def read():
    with session_scope(write=False) as db:
        row = db.get(Control, KEY)
        return dict(row.data) if row else {}


def save(state):
    with session_scope() as db:
        db.merge(Control(key=KEY, data=state))


def complete_bands(bands):
    try:
        if tuple(b["prefix_tokens"] for b in bands) != BANDS:
            return False
        for b in bands:
            if len(b["runs"]) != 5 or b["warm_p50_ms"] >= b["cold_p50_ms"]:
                return False
            for side in ("warm", "cold"):
                samples = [r[side] for r in b["runs"]]
                if any(x["prompt_tokens"] != b["prompt_tokens"] or not 0 < x["ttft_ms"] < 180000 for x in samples):
                    return False
                if any((x["cached_tokens"] != 0 if side == "cold" else
                        not .95*b["prefix_tokens"] <= x["cached_tokens"] <= x["prompt_tokens"]) for x in samples):
                    return False
                if median(x["ttft_ms"] for x in samples) != b[side+"_p50_ms"]:
                    return False
        return True
    except (TypeError, KeyError, ValueError):
        return False


def current():
    saved = read()
    if saved.get("status") == "completed" and saved.get("source") == identity() and complete_bands(saved.get("bands", [])):
        return saved
    return None


def measure(state):
    try:
        with Engine() as engine:
            for length in BANDS:
                if identity() != state["source"]:
                    raise ValueError("The registered inference hardware changed during calibration")
                result = benchmark(engine, length, 5)
                if result["warm_p50_ms"] >= result["cold_p50_ms"]:
                    raise ValueError("Cold and warm medians did not separate")
                state["bands"].append(result)
                save(state)
        state.update(status="completed", measured_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        # Keep partial raw samples for diagnosis, but never activate an incomplete calibration.
        state.update(status="failed", error=type(exc).__name__)
    save(state)


def start():
    global _worker
    with _lock:
        source = identity()
        if not source:
            raise ValueError("A live registered GPU is required for transport calibration")
        prior = read()
        if prior.get("source") == source and prior.get("status") == "completed" and complete_bands(prior.get("bands", [])):
            return prior
        if _worker is not None and _worker.is_alive():
            return prior
        with session_scope(write=False) as db:
            if db.scalar(select(AgentRun.id).where(AgentRun.status.in_(["queued","running"])).limit(1)):
                raise ValueError("Wait for current inference runs before measuring cold cache latency")
        state = {"source":source, "status":"running", "bands":[], "started":datetime.now(timezone.utc).isoformat()}
        save(state)
        _worker = Thread(target=measure,args=(state,),daemon=True,name="inference-route-calibration")
        _worker.start()
        return {"source":source,"status":"running","started":state["started"]}


def apply(local_profile, route):
    if not route:
        return local_profile
    # The original local prefill fit attributes GPU work. The deployed transport measures customer latency.
    return {**local_profile, "bands": route["bands"], "measured_at": route["measured_at"],
            "latency_source":"deployed API to registered GPU", "compute_measured_at":local_profile["measured_at"]}
