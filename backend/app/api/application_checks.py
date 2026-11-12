"""Only a trusted worker may claim or attest isolated HTTP checks."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.app import application_checks
from backend.app.api.access import admin, current_workspace, gateway, owned
from backend.app.db import session_scope
from backend.app.models import AgentRun

router = APIRouter()


class Worker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")


class Result(Worker):
    workspace_id: str = Field(min_length=1, max_length=64)
    lease_token: str = Field(min_length=32, max_length=128)
    answer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt: dict | None = None
    error: str | None = Field(default=None, min_length=1, max_length=400)


@router.get("/api/agent/runs/{identifier}/application-check", dependencies=[Depends(gateway)])
def read_check(identifier: str, ws=Depends(current_workspace)):
    from backend.app.api.finance import saved_application_check
    with session_scope(write=False) as db:
        run = owned(db, AgentRun, identifier, ws)
        current = application_checks.target(db, run)
        return {"run_id": run.id,
                "answer_sha256": current["answer_sha256"] if current else None,
                **saved_application_check(db, run)}


@router.post("/api/admin/application-checks/enqueue/{identifier}", dependencies=[Depends(admin)])
def enqueue_check(identifier: str):
    with session_scope() as db:
        run = db.get(AgentRun, identifier)
        if run is None:
            raise HTTPException(404, "Run was not found")
        application_checks.enqueue(db, run, required=True)
        return application_checks.evidence(db, run)


@router.post("/api/admin/application-checks/claim", dependencies=[Depends(admin)])
def claim_check(body: Worker):
    with session_scope() as db:
        return application_checks.claim(db, body.worker_id)


@router.post("/api/admin/application-checks/{identifier}/result", dependencies=[Depends(admin)])
def submit_check(identifier: str, body: Result):
    with session_scope() as db:
        return application_checks.submit(db, identifier, body.model_dump())


@router.get("/api/admin/application-checks/{identifier}", dependencies=[Depends(admin)])
def read_admin_check(identifier: str):
    with session_scope(write=False) as db:
        run = db.get(AgentRun, identifier)
        if run is None:
            raise HTTPException(404, "Run was not found")
        return application_checks.evidence(db, run)
