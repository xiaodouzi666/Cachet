from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from backend.app.db import init_db, session_scope
from backend.app.models import Consumer, Mandate, Workspace, utcnow


def test_file_migration_and_restart(tmp_path):
    url = f"sqlite:///{tmp_path}/durable.sqlite3"
    engine = init_db(url)
    assert {"workspace", "mandate", "ledger_entry", "capture_allocation"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar_one() == 2
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    with session_scope(engine) as db:
        workspace = Workspace(cookie_id=uuid4().hex, kind="judge")
        db.add(workspace)
        db.flush()
        workspace_id = workspace.id
    engine.dispose()
    with session_scope(init_db(url), write=False) as db:
        row = db.get(Workspace, workspace_id)
        assert row.id == workspace_id
        assert row.created_at.utcoffset() == timedelta(0)
        assert abs(utcnow() - row.created_at) < timedelta(minutes=1)


def test_foreign_keys_prevent_cross_workspace_mandate(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path}/test.sqlite3")
    with session_scope(engine) as db:
        first, other = [
            Workspace(cookie_id=uuid4().hex, kind="judge") for _ in range(2)
        ]
        db.add_all([first, other])
        db.flush()
        person = Consumer(
            workspace_id=first.id, salt_hex=uuid4().hex[:8], name="Unit test"
        )
        db.add(person)
        db.flush()
    with pytest.raises(IntegrityError):
        with session_scope(engine) as db:
            db.add(
                Mandate(
                    workspace_id=other.id, consumer_id=person.id, cap_micros=1_000_000
                )
            )
    with session_scope(engine, write=False) as db:
        assert not db.scalars(select(Mandate)).first()
