"""Retain prior private visibility and reconstruct layer usage from existing native call records."""

from sqlalchemy import select

from backend.app.models import Call, Context, MarketListing, RoyaltyUsage


def upgrade(connection):
    MarketListing.__table__.create(connection, checkfirst=True)
    RoyaltyUsage.__table__.create(connection, checkfirst=True)
    contexts = {
        (row.workspace_id, row.slug, row.publisher_id): row.id
        for row in connection.execute(
            select(Context.id, Context.workspace_id, Context.slug, Context.publisher_id)
        )
    }
    for row in connection.execute(
        select(Call.id, Call.workspace_id, Call.spans_json, Call.created_at)
    ):
        for span in row.spans_json:
            if span.get("kind") != "publisher":
                continue
            key = row.workspace_id, span.get("slug"), span.get("owner")
            if key not in contexts:
                raise ValueError(
                    "Cannot identify the published layer of an existing call"
                )
            connection.execute(
                RoyaltyUsage.__table__.insert().values(
                    context_id=contexts[key],
                    call_id=row.id,
                    hits=span["hits"],
                    gross_micros=span["royalty_micros"],
                    linear_micros=span["linear_micros"],
                    self_use=bool(span.get("self_royalty_suppressed")),
                    measured_at=row.created_at,
                )
            )
