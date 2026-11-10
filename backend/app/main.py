from contextlib import asynccontextmanager
import os
import subprocess
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
import httpx

from backend.app.config import ROOT, calibration, value
from backend.app.db import init_db
from backend.app.api import agent,catalog,copilot,finance,guide,webhooks,inference_endpoint
from backend.app.gateway.registration import monitor as monitor_endpoint
from backend.app.gateway.modes import health as engine_health
from backend.app.paypal.client import PayPalConfigError,PayPalError
from backend.app.settlement.watchdog import recovery,start


@asynccontextmanager
async def lifespan(app):
    init_db();recovery();timer=start();endpoint_timer=monitor_endpoint()
    try:yield
    finally:
        endpoint_timer.set()
        timer.set()


app=FastAPI(title="Cachet",version="0.2.0",lifespan=lifespan)
for module in (catalog,agent,copilot,finance,guide,webhooks,inference_endpoint):app.include_router(module.router)

@app.exception_handler(PayPalConfigError)
async def config_error(request:Request,exc):
    return JSONResponse({"detail":str(exc),"service":"paypal_sandbox"},status_code=503)


@app.exception_handler(PayPalError)
async def paypal_error(request:Request,exc):
    return JSONResponse({"detail":str(exc),"paypal_debug_id":exc.debug_id,"issue":exc.issue},
                        status_code=429 if exc.status_code==429 else 502)


@app.exception_handler(httpx.HTTPError)
async def connection_error(request:Request,exc):
    return JSONResponse({"detail":"Upstream service unavailable. Check the connection and retry."},status_code=503)


@app.exception_handler(ValueError)
async def invalid(request:Request,exc):
    return JSONResponse({"detail":str(exc)},status_code=422)


def revision():
    configured=os.getenv("RENDER_GIT_COMMIT")
    if configured:return configured[:12]
    if not (ROOT / ".git").exists():return None
    r=subprocess.run(["git","rev-parse","--short","HEAD"],cwd=ROOT,capture_output=True,text=True)
    return r.stdout.strip() or None


@app.get("/api/health")
def health():
    state=engine_health()
    return {"service":"cachet-api","version":app.version,"build":revision(),**state,
            "gpu":state.get("gpu") or (calibration()["gpu"] if state["mode"]=="live" else None),
            "paypal_configured":bool(value("PAYPAL_ENV","sandbox")=="sandbox" and value("PAYPAL_CLIENT_ID") and value("PAYPAL_CLIENT_SECRET")),
            "card_enabled":bool(value("SANDBOX_TEST_CARD_NUMBER") and value("SANDBOX_TEST_CARD_EXPIRY"))}
