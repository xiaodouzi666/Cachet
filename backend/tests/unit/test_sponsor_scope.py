from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app import ledger, marketplace
from backend.app.db import session_scope
from backend.app.models import Context, Publisher, Workspace
from backend.app.operations import Control


def test_operator_sponsor_can_only_fund_its_two_configured_private_publishers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "scope.sqlite"))
    for slug in ("starlette", "fastapi"):
        monkeypatch.setenv(
            "PUBLISHER_" + slug.upper() + "_EMAIL", slug + "@example.test"
        )
    with session_scope() as db:
        viewer, sponsor, other = [
            Workspace(kind=k, cookie_id=uuid4().hex)
            for k in ("judge", "sponsor", "judge")
        ]
        db.add_all([viewer, sponsor, other])
        db.flush()
        owners = []
        contexts = []
        for ws, slug in (
            (viewer, "starlette"),
            (viewer, "fastapi"),
            (other, "starlette"),
        ):
            publisher = Publisher(
                workspace_id=ws.id,
                name=slug,
                paypal_receiver_email=slug + "@example.test",
            )
            db.add(publisher)
            db.flush()
            owners.append(publisher)
            ctx = Context(
                workspace_id=ws.id,
                publisher_id=publisher.id,
                slug=slug,
                title=slug,
                source_url="https://example.test",
                license="MIT",
                version="unit",
                tokenizer="unit",
                token_len=1,
                token_hash="unit",
                text_path="unit",
                status="cold",
            )
            db.add(ctx)
            db.flush()
            contexts.append(ctx)
        db.add(
            Control(
                key="sponsor-target:" + sponsor.id,
                data={
                    "viewer": viewer.id,
                    "contexts": [c.id for c in contexts[:2]],
                    "publishers": [p.id for p in owners[:2]],
                },
            )
        )
        db.flush()
        assert {
            c.id for c in db.scalars(marketplace.visible_contexts(db, sponsor.id))
        } == {c.id for c in contexts[:2]}
        assert {
            c.id for c in db.scalars(marketplace.visible_contexts(db, other.id))
        } == {contexts[2].id}
        assert [marketplace.sponsored_publisher(db, sponsor.id, p) for p in owners] == [
            True,
            True,
            False,
        ]
        with pytest.raises(ledger.LedgerError, match="cross-workspace"):
            ledger.post(
                db,
                sponsor.id,
                "call",
                uuid4().hex,
                {"platform:revenue": 1, ledger.payable(owners[2].id): -1},
            )
        owners[0].paypal_receiver_email = "different@example.test"
        assert not marketplace.sponsored_publisher(db, sponsor.id, owners[0])
        with pytest.raises(ledger.LedgerError, match="cross-workspace"):
            ledger.post(
                db,
                sponsor.id,
                "call",
                uuid4().hex,
                {"platform:revenue": 1, ledger.payable(owners[0].id): -1},
            )
