import hashlib
import secrets
import time
from fastapi import Cookie, Header, HTTPException, Request
from sqlalchemy import delete, func, select
from backend.app.config import value
from backend.app.db import session_scope
from backend.app.models import Workspace
from backend.app.operations import LimitEvent


def admin(x_admin_token: str | None = Header(default=None)):
    expected = value("ADMIN_TOKEN")
    if not expected:
        raise HTTPException(503, "Configure the backend admin token")
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(403, "This action requires the trusted application gateway")


def gateway(x_gateway_token: str | None = Header(default=None),
            x_admin_token: str | None = Header(default=None)):
    """Workspace routes also accept the web service's limited credential."""
    expected = value("BFF_TOKEN")
    if expected and x_gateway_token and secrets.compare_digest(x_gateway_token, expected):
        return
    admin(x_admin_token)


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def current_workspace(cachet_workspace: str | None = Cookie(default=None)):
    if cachet_workspace and len(cachet_workspace) <= 256:
        with session_scope(write=False) as db:
            ws = db.scalar(
                select(Workspace).where(Workspace.cookie_id == digest(cachet_workspace))
            )
            if ws:
                return ws.id
    raise HTTPException(401, "Start a workspace to continue")


def owned(db, model, identifier, workspace_id):
    obj = db.get(model, identifier)
    if obj is None or obj.workspace_id != workspace_id:
        raise HTTPException(404, "Record was not found in this workspace")
    return obj


def limit(db, buckets, count, window):
    timestamp = int(time.time())
    db.execute(delete(LimitEvent).where(LimitEvent.at < timestamp - 86400))
    for bucket in buckets:
        recent = db.scalar(
            select(func.count())
            .select_from(LimitEvent)
            .where(LimitEvent.bucket == bucket, LimitEvent.at > timestamp - window)
        )
        if recent >= count:
            raise HTTPException(
                429,
                "Rate limit reached. Try again later.",
                headers={"Retry-After": str(window)},
            )
    db.add_all([LimitEvent(bucket=bucket, at=timestamp) for bucket in buckets])


def client_ip(request: Request):
    # The trusted BFF may pass its ingress client address. Store a digest only.
    return digest(
        request.headers.get("x-cachet-client-ip")
        or (request.client.host if request.client else "unknown")
    )
