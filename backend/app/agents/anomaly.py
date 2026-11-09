"""Workspace-scoped signals computed from actual meter readings."""

import statistics
from datetime import timedelta

from sqlalchemy import select

from ..models import Call, Publisher, utcnow


def volume_signal(observed, history):
    if len(history) < 7:
        return None
    deviation = statistics.pstdev(history)
    if not deviation:
        return None
    return (observed - statistics.mean(history)) / deviation


def activity(db, workspace_id, now=None):
    now = now or utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = now - today
    start = today - timedelta(days=7)
    publishers = list(db.scalars(select(Publisher).where(Publisher.workspace_id == workspace_id)))
    counts = {p.id: [0] * 8 for p in publishers}
    calls = db.scalars(select(Call).where(Call.workspace_id == workspace_id, Call.created_at.between(start, now)))
    for call in calls:
        day = (call.created_at - start).days
        if call.created_at - (start + timedelta(days=day)) > elapsed:
            continue
        for span in call.spans_json:
            if span["kind"] == "publisher" and span["owner"] in counts:
                hits = max(0, min(span["end"], call.cached_tokens) - span["start"])
                counts[span["owner"]][day] += hits
    return [
        {
            "publisher_id": p.id, "name": p.name, "today_hits": counts[p.id][-1],
            "reference_hits": counts[p.id][:-1],
            "z_score": volume_signal(counts[p.id][-1], counts[p.id][:-1]) if p.created_at <= start else None,
        }
        for p in publishers
    ]
