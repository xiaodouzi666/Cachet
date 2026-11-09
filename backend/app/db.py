from contextlib import contextmanager
from functools import lru_cache
from importlib import import_module
from importlib.util import find_spec
import os
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from backend.migrations import v001_initial, v002_marketplace


@lru_cache(maxsize=8)
def engine_for(url: str):
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("Cachet requires a SQLite database")
    if parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30}
    )

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.execute("PRAGMA busy_timeout=30000")

    return engine


def configured_url():
    root = Path(__file__).resolve().parents[2]
    return (
        os.environ.get("DATABASE_URL")
        or dotenv_values(root / ".env").get("DATABASE_URL")
        or f"sqlite:///{root / '.cache' / 'cachet.db'}"
    )


@lru_cache(maxsize=8)
def _migrate(url):
    # The API owns operational tables (run events, etc.) on the same metadata.
    if find_spec("backend.app.operations"):
        import_module("backend.app.operations")
    engine = engine_for(url)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
            if version == 0:
                if inspect(conn).get_table_names():
                    raise RuntimeError(
                        "Refusing to adopt an unversioned nonempty database"
                    )
                v001_initial.upgrade(conn)
                version = 1
            if version == 1:
                v002_marketplace.upgrade(conn)
                conn.exec_driver_sql("PRAGMA user_version=2")
            elif version != 2:
                raise RuntimeError(f"Unsupported database schema version {version}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return engine


def init_db(url=None):
    return _migrate(url or configured_url())


@contextmanager
def session_scope(engine=None, *, write=True):
    """A short transaction. Keep external calls outside this context."""
    with Session(
        engine if engine is not None else init_db(), expire_on_commit=False
    ) as session:
        # Explicit BEGIN gives read transactions a stable snapshot. IMMEDIATE serializes
        # writers before checking balances or obtaining an idempotency decision.
        session.connection().exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield session
            if write:
                from .ledger import assert_invariants

                assert_invariants(session)
                session.commit()
            else:
                # End the read transaction without expiring attributes of returned records.
                if session.new or session.dirty or session.deleted:
                    raise RuntimeError("A read-only database scope cannot write")
                session.expunge_all()
                session.rollback()
        except Exception:
            session.rollback()
            raise
