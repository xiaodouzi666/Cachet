"""Preview and confirmation use one allocation and reject a changed total before payment."""
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from backend.app import ledger
from backend.app.api.finance import settlement_preview
from backend.app.db import session_scope
from backend.app.models import PayoutBatch, Publisher, Workspace
from backend.app.settlement.payout_job import settle


def test_judge_can_review_both_recipients_but_cannot_confirm_a_stale_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/review.db")
    with session_scope() as db:
        judge, other = (Workspace(cookie_id=uuid4().hex, kind=kind) for kind in ("judge", "user"))
        db.add_all([judge, other])
        db.flush()
        pubs = [Publisher(workspace_id=judge.id, name=name, paypal_receiver_email=f"{name}@example.invalid")
                for name in ("Starlette", "FastAPI")]
        db.add_all(pubs)
        db.flush()
    amounts = {pubs[0].id: 255_040, pubs[1].id: 22_800}
    monkeypatch.setattr(ledger, "available_payable", lambda db, ws, pid: amounts[pid])
    quote = settlement_preview(judge.id)
    assert quote["amount_cents"] == 27
    assert {(r["name"], r["amount_cents"]) for r in quote["items"]} == {("Starlette", 25), ("FastAPI", 2)}
    with pytest.raises(HTTPException) as denied:
        settlement_preview(other.id)
    assert denied.value.status_code == 403
    amounts[pubs[1].id] = 9_999
    with pytest.raises(HTTPException) as changed:
        settle(judge.id, quote_id=quote["quote_id"])
    assert changed.value.status_code == 409
    assert settlement_preview(judge.id)["amount_cents"] == 25
    with session_scope(write=False) as db:
        assert db.scalar(select(func.count()).select_from(PayoutBatch)) == 0
