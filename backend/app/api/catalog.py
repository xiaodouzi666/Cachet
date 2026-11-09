from threading import Event, Lock, Thread
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parseaddr
from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from backend.app import marketplace
from backend.app.api.access import admin, current_workspace, owned
from backend.app.config import calibration, manifest, sources, value
from backend.app.db import session_scope
from backend.app.models import Context, Publisher
from backend.app.gateway.prompt_builder import Prefix
from backend.app.gateway.quote import quote
from backend.app.gateway.sglang_client import Engine

router = APIRouter(prefix="/api", dependencies=[Depends(admin)])
wsdep = Depends(current_workspace)


class PublisherInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    paypal_email: str = Field(min_length=3, max_length=254)


class PublishInput(BaseModel):
    publisher_id: str
    source_slug: str
    parent_id: str | None = None


class QuoteInput(BaseModel):
    context_id: str
    est_turns: int = Field(default=10, ge=1, le=30)
    cold_fraction: Decimal = Field(
        default_factory=lambda: Decimal(value("COLD_FRACTION_DEFAULT", "0.25")),
        ge=0,
        le=1,
    )


@router.get("/sources")
def listed_sources():
    return [
        dict(
            slug=r["slug"],
            title=r["slug"].capitalize(),
            parent=r["parent"],
            token_len=r["token_len"],
            license=manifest()["projects"][r["slug"]]["license"],
        )
        for r in sources().values()
    ]


@router.get("/benchmarks")
def benchmarks():
    c = calibration()
    return [
        dict(
            prefix_len=b["prefix_tokens"],
            cold_ttft_ms_p50=b["cold_p50_ms"],
            warm_ttft_ms_p50=b["warm_p50_ms"],
            gpu=c["gpu"],
            model=c["model"],
            sglang=c["sglang"],
            measured_at=c["measured_at"],
            trials=len(b["runs"]),
        )
        for b in c["bands"]
    ]


@router.post("/publishers")
def publish_owner(body: PublisherInput, ws=wsdep):
    email = body.paypal_email.strip()
    if (
        parseaddr(email)[1] != email
        or email.count("@") != 1
        or "." not in email.rpartition("@")[2]
    ):
        raise HTTPException(422, "A valid sandbox receiver email is required")
    if not body.name.strip():
        raise HTTPException(422, "Publisher name is required")
    with session_scope() as db:
        existing = db.scalar(
            select(Publisher).where(
                Publisher.workspace_id == ws, Publisher.paypal_receiver_email == email
            )
        )
        if existing:
            return {"id": existing.id, "name": existing.name}
        obj = Publisher(
            workspace_id=ws, name=body.name.strip(), paypal_receiver_email=email
        )
        db.add(obj)
        db.flush()
        return {"id": obj.id, "name": obj.name}


def viewer(cachet_workspace: str | None = Cookie(default=None)):
    return current_workspace(cachet_workspace) if cachet_workspace else None


def chain(db, ctx):
    parent = marketplace.parent_id(db, ctx)
    return (chain(db, db.get(Context, parent)) if parent else []) + [ctx]


def view(db, c, workspace_id):
    hits, royalties, _ = marketplace.totals(db, c.id)
    return {
        "id": c.id,
        "title": c.title,
        "slug": c.slug,
        "parent_id": marketplace.parent_id(db, c),
        "can_manage": c.workspace_id == workspace_id,
        "publisher_id": c.publisher_id,
        "publisher_name": db.get(Publisher, c.publisher_id).name,
        "status": c.status,
        "token_len": c.token_len,
        "last_probe_hit_ratio": c.last_probe_hit_ratio,
        "last_probe_at": c.last_probe_at,
        "historical_totals_complete": marketplace.complete_history(db, c.id),
        "total_hits": hits,
        "royalty_micros": royalties,
        "price_shared": value("PRICE_SHARED", "0.15"),
        "license": c.license,
    }


def warm(context_id, ws):
    with session_scope(write=False) as db:
        slug = owned(db, Context, context_id, ws).slug
    try:
        with Engine() as eng:
            ratio = Prefix(eng, slug).warm(eng)
    except Exception:
        with session_scope() as db:
            c = owned(db, Context, context_id, ws)
            c.status = "cold"
            c.last_probe_hit_ratio = None
        return
    with session_scope() as db:
        c = owned(db, Context, context_id, ws)
        c.status = "hot" if ratio >= 0.95 else "cold"
        c.last_probe_hit_ratio = ratio
        c.last_probe_at = datetime.now(timezone.utc)


@router.post("/contexts")
def publish(body: PublishInput, bg: BackgroundTasks, ws=wsdep):
    row = sources().get(body.source_slug)
    if not row:
        raise HTTPException(422, "Unknown licensed source")
    with session_scope() as db:
        pub = owned(db, Publisher, body.publisher_id, ws)
        parent = (
            marketplace.visible_context(db, body.parent_id, ws)
            if body.parent_id
            else None
        )
        if (parent.slug if parent else None) != row["parent"]:
            raise HTTPException(422, "Select the published parent layer")
        c = db.scalar(
            select(Context).where(
                Context.workspace_id == ws, Context.slug == row["slug"]
            )
        )
        if c:
            if c.publisher_id != pub.id:
                raise HTTPException(409, "This workspace already published the source")
            marketplace.publish(db, c, parent)
            return view(db, c, ws)
        project = manifest()["projects"][row["slug"]]
        c = Context(
            workspace_id=ws,
            publisher_id=pub.id,
            parent_id=parent.id if parent and parent.workspace_id == ws else None,
            slug=row["slug"],
            title=row["slug"].capitalize(),
            source_url=project["source_url"],
            license=project["license"],
            version=project["revision"],
            tokenizer=manifest()["tokenizer"]["revision"],
            token_len=row["token_len"],
            token_hash=row["ids_sha256"],
            text_path=row["text_path"],
            status="warming",
        )
        db.add(c)
        db.flush()
        marketplace.publish(db, c, parent)
        bg.add_task(warm, c.id, ws)
        return view(db, c, ws)


@router.get("/contexts")
def catalog(ws=Depends(viewer)):
    with session_scope(write=False) as db:
        return [
            view(db, c, ws) for c in db.scalars(marketplace.visible_contexts(db, ws))
        ]


@router.get("/contexts/{identifier}")
def context(identifier: str, ws=Depends(viewer)):
    with session_scope(write=False) as db:
        return view(db, marketplace.visible_context(db, identifier, ws), ws)


@router.post("/contexts/{identifier}/warm")
def rewarm(identifier: str, bg: BackgroundTasks, ws=wsdep):
    with session_scope() as db:
        ctx = owned(db, Context, identifier, ws)
        ctx.status = "warming"
        bg.add_task(warm, identifier, ws)
        return view(db, ctx, ws)


@router.post("/quote")
def quote_context(body: QuoteInput, ws=Depends(viewer)):
    with session_scope(write=False) as db:
        ctx = marketplace.visible_context(db, body.context_id, ws)
        return quote(
            sum(x.token_len for x in chain(db, ctx)), body.est_turns, body.cold_fraction
        )


_rewarm_lock = Lock()
_after_flush = None


def observed_live_call():
    with _rewarm_lock:
        if _after_flush is not None:
            _after_flush.set()


def schedule_rewarm():
    """Keep the next real call cold, then restore published prefixes even if nobody runs."""
    global _after_flush
    observation = Event()
    with _rewarm_lock:
        _after_flush = observation
    with session_scope() as db:
        contexts = list(db.scalars(select(Context)))
        refs = [(c.id, c.workspace_id) for c in contexts]
        for context in contexts:
            context.status = "cold"
            context.last_probe_hit_ratio = None

    def restore():
        global _after_flush
        observation.wait(timeout=120)
        for identifier, workspace in refs:
            warm(identifier, workspace)
        with _rewarm_lock:
            if _after_flush is observation:
                _after_flush = None

    if refs:
        Thread(target=restore, daemon=True, name="restore-prefix-cache").start()
    return len(refs)
