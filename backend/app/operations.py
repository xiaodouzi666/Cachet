"""Durable cross-request coordination, keyed by the authenticated workspace."""

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Integer, JSON, String
from backend.app.models import Base


def now():
    return datetime.now(timezone.utc)


class RunEvent(Base):
    __tablename__ = "run_events"
    id = Column(Integer, primary_key=True)
    run_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)
    payload = Column(JSON, nullable=False)


class Operation(Base):
    """Persist a provider request identity and exact payload before sending any money."""

    __tablename__ = "provider_operations"
    id = Column(String, primary_key=True)
    workspace_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)
    subject = Column(String, nullable=False, index=True)
    payload = Column(JSON, nullable=False)
    external_id = Column(String, nullable=True)
    result = Column(JSON(none_as_null=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now, nullable=False)


class Control(Base):
    __tablename__ = "workspace_controls"
    key = Column(String, primary_key=True)
    data = Column(JSON, nullable=False)


class LimitEvent(Base):
    __tablename__ = "rate_events"
    id = Column(Integer, primary_key=True)
    bucket = Column(String, index=True, nullable=False)
    at = Column(Integer, index=True, nullable=False)
