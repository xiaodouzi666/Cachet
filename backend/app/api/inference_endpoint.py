from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.app.api.access import admin
from backend.app.gateway import registration

router = APIRouter()


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=10, max_length=255)
    sglang_version: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=160)
    gpu: str = Field(min_length=1, max_length=160)


@router.post("/api/admin/inference-endpoint", dependencies=[Depends(admin)])
def update_endpoint(body: Heartbeat):
    if not registration.enabled():
        raise HTTPException(409, "Registered GPU discovery is disabled on this backend.")
    return registration.heartbeat(body.model_dump())


@router.get("/api/admin/inference-endpoint", dependencies=[Depends(admin)])
def current_endpoint():
    state = registration.record() if registration.enabled() else {}
    return {"live": bool(state and registration.active()),
            **{k: state[k] for k in ("url", "gpu", "model", "sglang_version", "seen_at", "checked_at") if k in state}}


@router.post("/api/admin/inference-calibration", dependencies=[Depends(admin)])
def calibrate_transport():
    from backend.app.gateway.route_calibration import start
    return start()


@router.get("/api/admin/inference-calibration", dependencies=[Depends(admin)])
def calibration_status():
    from backend.app.gateway.route_calibration import read
    return read()
