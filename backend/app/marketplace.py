"""Public catalog and aggregate statistics. Payer records stay in the buyer's workspace."""

from fastapi import HTTPException
from sqlalchemy import func, or_, select

from .models import Call, Context, MarketListing, RoyaltyUsage, Workspace
from .operations import Control


def visible_contexts(db, workspace_id):
    public = Context.id.in_(
        select(MarketListing.context_id)
    ) & Context.workspace_id.in_(select(Workspace.id).where(Workspace.kind == "user"))
    if workspace_id is None:
        clause = public
    elif db.get(Workspace, workspace_id).kind == "sponsor":
        grant = db.get(Control, "sponsor-target:" + workspace_id)
        allowed = grant.data if grant else {}
        clause = (Context.workspace_id == allowed.get("viewer")) & Context.id.in_(allowed.get("contexts", []))
    elif db.get(Workspace, workspace_id).kind == "judge":
        clause = Context.workspace_id == workspace_id
    else:
        clause = or_(public, Context.workspace_id == workspace_id)
    return select(Context).where(clause)


def visible_context(db, identifier, workspace_id):
    context = db.scalar(
        visible_contexts(db, workspace_id).where(Context.id == identifier)
    )
    if context is None:
        raise HTTPException(404, "Published context was not found")
    return context


def sponsored_publisher(db, sponsor_id, publisher):
    """Authorize only the two configured recipients registered to this one preparation job."""
    from .config import value
    grant = db.get(Control, "sponsor-target:" + sponsor_id)
    if not grant or grant.data.get("viewer") != publisher.workspace_id:
        return False
    for slug, pid, cid in zip(("starlette", "fastapi"), grant.data["publishers"], grant.data["contexts"], strict=True):
        if pid != publisher.id:
            continue
        expected = value("PUBLISHER_" + slug.upper() + "_EMAIL").strip().casefold()
        context = db.get(Context, cid)
        return bool(expected and publisher.paypal_receiver_email.strip().casefold() == expected and context
                    and context.workspace_id == publisher.workspace_id and context.publisher_id == pid and context.slug == slug)
    return False


def parent_id(db, ctx):
    listing = db.get(MarketListing, ctx.id)
    return listing.parent_id if listing else ctx.parent_id


def publish(db, ctx, parent):
    if db.get(Workspace, ctx.workspace_id).kind != "user":
        return
    if parent is not None and db.get(MarketListing, parent.id) is None:
        raise ValueError("Publish the selected parent in the public marketplace first")
    existing = db.get(MarketListing, ctx.id)
    parent_identifier = parent.id if parent else None
    if existing:
        if existing.parent_id != parent_identifier:
            raise ValueError("A published layer already references a different parent")
        return
    db.add(MarketListing(context_id=ctx.id, parent_id=parent_identifier))
    db.flush()


def complete_history(db, context_id):
    return db.get(Control, f"partial-layer-history:{context_id}") is None


def totals(db, context_id, *, buyer=None):
    u = RoyaltyUsage
    query = select(
        *[
            func.coalesce(func.sum(column), 0)
            for column in (u.hits, u.gross_micros, u.linear_micros)
        ]
    ).where(u.context_id == context_id)
    if buyer is not None:
        query = query.join(Call, Call.id == u.call_id).where(Call.workspace_id == buyer)
    return db.execute(query).one()


def settlement_contexts(db, workspace_id):
    purchased = (
        select(RoyaltyUsage.context_id)
        .join(Call, Call.id == RoyaltyUsage.call_id)
        .where(Call.workspace_id == workspace_id)
    )
    return db.scalars(
        select(Context).where(
            or_(Context.workspace_id == workspace_id, Context.id.in_(purchased))
        )
    )


def record_usage(db, call, context_ids):
    """Bind each billed layer to its precise canonical listing before committing the journal."""
    spans = [s for s in call.spans_json if s["kind"] == "publisher"]
    if not spans:
        return
    if context_ids is None:
        # A task queued before the public-market migration can only use its original private sources.
        context_ids = [
            db.scalar(
                select(Context.id).where(
                    Context.workspace_id == call.workspace_id,
                    Context.slug == span["slug"],
                    Context.publisher_id == span["owner"],
                )
            )
            for span in spans
        ]
    if len(context_ids) != len(spans):
        raise ValueError("Charged public layers differ from the quoted chain")
    for context_id, span in zip(context_ids, spans):
        ctx = visible_context(db, context_id, call.workspace_id)
        if ctx.publisher_id != span["owner"] or ctx.slug != span["slug"]:
            raise ValueError("Charged publisher differs from the quoted public layer")
        db.add(
            RoyaltyUsage(
                context_id=ctx.id,
                call_id=call.id,
                hits=span["hits"],
                gross_micros=span["royalty_micros"],
                linear_micros=span["linear_micros"],
                self_use=bool(span.get("self_royalty_suppressed")),
                measured_at=call.created_at,
            )
        )
