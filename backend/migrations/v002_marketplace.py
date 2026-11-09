"""Preserve the v1 journals/operations and project only reproducible per-layer measures."""

from collections import Counter, defaultdict

from sqlalchemy import select

from backend.app.models import AgentRun, Call, Context, MarketListing, RoyaltyUsage
from backend.app.operations import Control


def upgrade(connection):
    MarketListing.__table__.create(connection, checkfirst=True)
    RoyaltyUsage.__table__.create(connection, checkfirst=True)
    contexts = defaultdict(list)
    for row in connection.execute(
        select(
            Context.id,
            Context.workspace_id,
            Context.slug,
            Context.publisher_id,
            Context.token_len,
        )
    ):
        contexts[row.workspace_id, row.publisher_id].append(row)
    calls = select(
        Call.id,
        Call.workspace_id,
        Call.spans_json,
        Call.created_at,
        Call.cached_tokens,
        Call.royalty_json,
        Call.linear_royalty_json,
        AgentRun.quote_json,
    ).join(AgentRun, AgentRun.id == Call.run_id)
    incomplete = set()
    for call in connection.execute(calls):
        spans = [s for s in call.spans_json if s.get("kind") == "publisher"]
        multiplicity = Counter(s["owner"] for s in spans)
        chain = (call.quote_json or {}).get("chain", [])
        for i, span in enumerate(spans):
            publisher = span["owner"]
            slug = span.get("slug") or (chain[i] if i < len(chain) else None)
            rows = contexts[call.workspace_id, publisher]
            candidates = [
                c
                for c in rows
                if c.token_len == span["end"] - span["start"]
                and (slug is None or c.slug == slug)
            ]
            gross, linear = span.get("royalty_micros"), span.get("linear_micros")
            if multiplicity[publisher] == 1:
                if gross is None:
                    gross = call.royalty_json.get(publisher)
                if linear is None:
                    linear = call.linear_royalty_json.get(publisher)
            hits = max(0, min(call.cached_tokens, span["end"]) - span["start"])
            if hits == 0:
                gross = 0 if gross is None else gross
                linear = 0 if linear is None else linear
            if len(candidates) != 1 or gross is None or linear is None:
                # V1 allowed multiple layers under one payee, with only a combined royalty.
                # Keep its original journal and call intact and explicitly flag incomplete totals.
                incomplete.update(c.id for c in (candidates or rows))
                continue
            connection.execute(
                RoyaltyUsage.__table__.insert().values(
                    context_id=candidates[0].id,
                    call_id=call.id,
                    hits=hits,
                    gross_micros=gross,
                    linear_micros=linear,
                    self_use=bool(span.get("self_royalty_suppressed")),
                    measured_at=call.created_at,
                )
            )
    for context_id in incomplete:
        connection.execute(
            Control.__table__.insert().values(
                key=f"partial-layer-history:{context_id}",
                data={
                    "reason": "v1 stored insufficient information to attribute all historical layers"
                },
            )
        )
