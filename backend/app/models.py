"""Cachet's SQLite schema. Financial and tenant identifiers are separate namespaces."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow():
    return datetime.now(timezone.utc)


def new_id():
    return uuid4().hex


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if value.tzinfo is None:
            raise ValueError("Timestamp requires a timezone")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class Base(DeclarativeBase):
    pass


class Entity:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ScopedEntity(Entity):
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)


def scoped_fk(column, table):
    return ForeignKeyConstraint([column, "workspace_id"], [f"{table}.id", f"{table}.workspace_id"])


class Workspace(Entity, Base):
    __tablename__ = "workspace"
    cookie_id: Mapped[str] = mapped_column(unique=True)
    kind: Mapped[str] = mapped_column(default="judge")


class Publisher(ScopedEntity, Base):
    __tablename__ = "publisher"
    __table_args__ = (UniqueConstraint("id", "workspace_id"),)
    name: Mapped[str]
    paypal_receiver_email: Mapped[str]
    linked_payer_id: Mapped[str | None]


class Context(ScopedEntity, Base):
    __tablename__ = "context"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id"), UniqueConstraint("workspace_id", "slug"),
        scoped_fk("parent_id", "context"), scoped_fk("publisher_id", "publisher"),
        CheckConstraint("token_len >= 0"),
    )
    publisher_id: Mapped[str]
    parent_id: Mapped[str | None]
    slug: Mapped[str]
    title: Mapped[str]
    source_url: Mapped[str]
    license: Mapped[str]
    version: Mapped[str]
    tokenizer: Mapped[str]
    token_len: Mapped[int]
    token_hash: Mapped[str]
    text_path: Mapped[str]
    status: Mapped[str] = mapped_column(default="draft")
    last_probe_hit_ratio: Mapped[float | None]
    last_probe_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Consumer(ScopedEntity, Base):
    __tablename__ = "consumer"
    __table_args__ = (UniqueConstraint("id", "workspace_id"),)
    name: Mapped[str]
    salt_hex: Mapped[str] = mapped_column(unique=True)
    linked_payer_id: Mapped[str | None]


class Mandate(ScopedEntity, Base):
    __tablename__ = "mandate"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id"), scoped_fk("consumer_id", "consumer"),
        CheckConstraint("cap_micros > 0 AND cap_micros % 10000 = 0 AND cap_micros <= 20000000"),
        CheckConstraint("captured_micros >= 0 AND accrued_uncaptured_micros >= 0 AND "
                        "captured_micros + accrued_uncaptured_micros <= cap_micros"),
        CheckConstraint("refunded_micros >= 0 AND refunded_micros <= captured_micros"),
    )
    consumer_id: Mapped[str] = mapped_column(index=True)
    paypal_order_id: Mapped[str | None] = mapped_column(unique=True)
    authorization_id: Mapped[str | None] = mapped_column(unique=True)
    cap_micros: Mapped[int]
    captured_micros: Mapped[int] = mapped_column(default=0)
    accrued_uncaptured_micros: Mapped[int] = mapped_column(default=0)
    refunded_micros: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(default="CREATED", index=True)
    payment_method: Mapped[str] = mapped_column(default="wallet")
    last_activity_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class AgentRun(ScopedEntity, Base):
    __tablename__ = "agent_run"
    __table_args__ = (UniqueConstraint("id", "workspace_id"), scoped_fk("mandate_id", "mandate"))
    mandate_id: Mapped[str] = mapped_column(index=True)
    task_id: Mapped[str]
    task_text: Mapped[str] = mapped_column(Text)
    decision: Mapped[str | None]
    quote_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    mode: Mapped[str | None]


class Call(ScopedEntity, Base):
    __tablename__ = "call"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id"), UniqueConstraint("run_id", "turn"), scoped_fk("run_id", "agent_run"),
        CheckConstraint("turn >= 0 AND prompt_tokens >= cached_tokens AND cached_tokens >= 0 AND output_tokens >= 0"),
        CheckConstraint("public_charge_micros >= 0 AND charge_micros >= public_charge_micros AND platform_micros >= 0"),
    )
    run_id: Mapped[str] = mapped_column(index=True)
    turn: Mapped[int]
    prompt_tokens: Mapped[int]
    cached_tokens: Mapped[int]
    spans_json: Mapped[list] = mapped_column(JSON)
    hits_json: Mapped[dict] = mapped_column(JSON)
    output_tokens: Mapped[int]
    input_hash: Mapped[str]
    output_text: Mapped[str] = mapped_column(Text)
    ttft_ms: Mapped[float]
    total_ms: Mapped[float]
    charge_micros: Mapped[int]
    public_charge_micros: Mapped[int]
    royalty_json: Mapped[dict] = mapped_column(JSON)
    linear_royalty_json: Mapped[dict] = mapped_column(JSON, default=dict)
    shapley_royalty_json: Mapped[dict] = mapped_column(JSON, default=dict)
    platform_micros: Mapped[int]
    sla_breach: Mapped[bool] = mapped_column(default=False)
    fault_injected_ms: Mapped[float] = mapped_column(default=0)
    mode: Mapped[str]
    reversed_micros: Mapped[int] = mapped_column(default=0)
    reversed_public_micros: Mapped[int] = mapped_column(default=0)
    reversed_royalty_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Capture(ScopedEntity, Base):
    __tablename__ = "capture"
    __table_args__ = (
        UniqueConstraint("id", "workspace_id"), scoped_fk("mandate_id", "mandate"),
        CheckConstraint("amount_cents > 0 AND fee_cents >= 0"),
    )
    mandate_id: Mapped[str] = mapped_column(index=True)
    paypal_capture_id: Mapped[str | None] = mapped_column(unique=True)
    amount_cents: Mapped[int]
    final: Mapped[bool]
    fee_cents: Mapped[int] = mapped_column(default=0)
    status: Mapped[str]
    call_ids_json: Mapped[list] = mapped_column(JSON, default=list)


class CaptureAllocation(ScopedEntity, Base):
    __tablename__ = "capture_allocation"
    __table_args__ = (
        UniqueConstraint("capture_id", "call_id"), scoped_fk("capture_id", "capture"), scoped_fk("call_id", "call"),
        CheckConstraint("principal_micros >= public_micros AND public_micros >= 0"),
        CheckConstraint("refunded_micros >= refunded_public_micros AND refunded_public_micros >= 0 AND "
                        "refunded_micros <= principal_micros AND refunded_public_micros <= public_micros"),
    )
    capture_id: Mapped[str] = mapped_column(index=True)
    call_id: Mapped[str] = mapped_column(index=True)
    principal_micros: Mapped[int]
    public_micros: Mapped[int]
    royalty_json: Mapped[dict] = mapped_column(JSON)
    refunded_micros: Mapped[int] = mapped_column(default=0)
    refunded_public_micros: Mapped[int] = mapped_column(default=0)
    refunded_royalty_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Refund(ScopedEntity, Base):
    __tablename__ = "refund"
    __table_args__ = (UniqueConstraint("id", "workspace_id"), scoped_fk("capture_id", "capture"),
                      CheckConstraint("amount_cents > 0 AND fee_reversal_cents >= 0"))
    capture_id: Mapped[str] = mapped_column(index=True)
    paypal_refund_id: Mapped[str | None] = mapped_column(unique=True)
    amount_cents: Mapped[int]
    fee_reversal_cents: Mapped[int] = mapped_column(default=0)
    reason: Mapped[str]
    call_ids_json: Mapped[list] = mapped_column(JSON)
    status: Mapped[str]


class PayoutBatch(ScopedEntity, Base):
    __tablename__ = "payout_batch"
    __table_args__ = (UniqueConstraint("id", "workspace_id"), CheckConstraint("fee_cents >= 0"))
    sender_batch_id: Mapped[str] = mapped_column(unique=True)
    paypal_batch_id: Mapped[str | None] = mapped_column(unique=True)
    status: Mapped[str]
    fee_cents: Mapped[int] = mapped_column(default=0)


class PayoutItem(ScopedEntity, Base):
    __tablename__ = "payout_item"
    __table_args__ = (
        UniqueConstraint("batch_id", "publisher_id"), scoped_fk("batch_id", "payout_batch"),
        scoped_fk("publisher_id", "publisher"), CheckConstraint("amount_cents > 0 AND fee_cents >= 0"),
    )
    batch_id: Mapped[str] = mapped_column(index=True)
    publisher_id: Mapped[str] = mapped_column(index=True)
    amount_cents: Mapped[int]
    fee_cents: Mapped[int] = mapped_column(default=0)
    paypal_item_id: Mapped[str | None] = mapped_column(unique=True)
    status: Mapped[str]


class LedgerTxn(ScopedEntity, Base):
    __tablename__ = "ledger_txn"
    __table_args__ = (UniqueConstraint("id", "workspace_id"), UniqueConstraint("ref_type", "ref_id"))
    ref_type: Mapped[str]
    ref_id: Mapped[str]


class LedgerEntry(ScopedEntity, Base):
    __tablename__ = "ledger_entry"
    __table_args__ = (
        UniqueConstraint("txn_id", "account"), scoped_fk("txn_id", "ledger_txn"),
        CheckConstraint("(debit_micros > 0 AND credit_micros = 0) OR (credit_micros > 0 AND debit_micros = 0)"),
    )
    txn_id: Mapped[str] = mapped_column(index=True)
    account: Mapped[str] = mapped_column(index=True)
    debit_micros: Mapped[int]
    credit_micros: Mapped[int]
    ref_type: Mapped[str]
    ref_id: Mapped[str]


class WebhookEvent(Entity, Base):
    __tablename__ = "webhook_event"
    paypal_event_id: Mapped[str] = mapped_column(unique=True)
    event_type: Mapped[str]
    resource_id: Mapped[str]
    verified: Mapped[bool]
    raw_json: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class BenchResult(Entity, Base):
    __tablename__ = "bench_result"
    __table_args__ = (UniqueConstraint("model", "gpu", "prefix_len"),)
    model: Mapped[str]
    gpu: Mapped[str]
    prefix_len: Mapped[int]
    cold_ttft_ms_p50: Mapped[float]
    warm_ttft_ms_p50: Mapped[float]
    runs_json: Mapped[list] = mapped_column(JSON)
