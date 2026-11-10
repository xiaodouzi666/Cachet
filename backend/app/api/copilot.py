import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agents.copilot import ModelFailure, reply
from ..agents.copilot_tools import confirm
from ..db import session_scope
from .access import gateway, client_ip, current_workspace, limit

router = APIRouter(prefix="/api", dependencies=[Depends(gateway)])
log = logging.getLogger(__name__)


class Message(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class Confirmation(BaseModel):
    proposal_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def event(name, data):
    return "event: " + name + "\ndata: " + json.dumps(data) + "\n\n"


@router.post("/copilot")
def query(body: Message, request: Request, ws=Depends(current_workspace)):
    if not body.message.strip():
        raise HTTPException(422, "Enter a question about this workspace.")
    with session_scope() as db:
        limit(db, [f"copilot:workspace:{ws}", f"copilot:ip:{client_ip(request)}"], 60, 3600)

    def stream():
        try:
            for name, data in reply(ws, body.message.strip()):
                yield event(name, data)
        except (ValueError, ModelFailure) as exc:
            yield event("error", {"message": str(exc)})
        except Exception:
            log.exception("Copilot evidence retrieval failed")
            yield event("error", {"message": "The evidence lookup could not be completed."})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/copilot/confirm")
def confirm_proposal(body: Confirmation, ws=Depends(current_workspace)):
    return confirm(ws, body.proposal_id)
