from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from backend.app.db import init_db, session_scope
from backend.app.ledger import (
    CLEARING, LedgerError, Parts, assert_invariants, balance, post, record_capture, record_payout, record_refund, refund_cents,
)
from backend.app.models import Capture, LedgerEntry, PayoutItem, Refund, Workspace


@pytest.fixture
def book(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path}/journal.sqlite3")
    with session_scope(engine) as db:
        ws = Workspace(cookie_id=uuid4().hex, kind="user")
        db.add(ws)
        db.flush()
    return engine, ws.id


def test_balanced_local_journal_is_atomic_and_idempotent(book):
    engine, wid = book
    ref = uuid4().hex
    changes = {"platform:revenue": -5000, "platform:rounding": 5000}
    with session_scope(engine) as db:
        assert post(db, wid, "unit_adjustment", ref, changes)
        assert not post(db, wid, "unit_adjustment", ref, changes)
    with pytest.raises(LedgerError):
        with session_scope(engine) as db:
            post(db, wid, "unit_adjustment", ref, {k: -v for k, v in changes.items()})
    with pytest.raises(LedgerError):
        with session_scope(engine) as db:
            post(db, wid, "unit_adjustment", uuid4().hex, {"platform:rounding": 1})
    with session_scope(engine, write=False) as db:
        assert db.scalar(select(func.count(LedgerEntry.id))) == 2
        assert balance(db, wid, "platform:revenue") == -5000
        assert assert_invariants(db, workspace_id=wid, expected_clearing_micros=0) == 0


def test_commit_rejects_unbacked_paypal_clearing_and_reconciliation_detects_difference(book):
    engine, wid = book
    with pytest.raises(LedgerError, match="clearing"):
        with session_scope(engine) as db:
            post(db, wid, "unit_adjustment", uuid4().hex, {CLEARING: 10000, "platform:rounding": -10000})
    with session_scope(engine, write=False) as db:
        assert balance(db, wid, CLEARING) == 0
        with pytest.raises(LedgerError, match="readbacks"):
            assert_invariants(db, workspace_id=wid, expected_clearing_micros=10000)


def test_commit_detects_imbalanced_edits(book):
    engine, wid = book
    with session_scope(engine) as db:
        post(db, wid, "unit_adjustment", uuid4().hex, {"platform:revenue": -7, "platform:rounding": 7})
    with pytest.raises(LedgerError, match="Unbalanced"):
        with session_scope(engine) as db:
            db.execute(text("UPDATE ledger_entry SET debit_micros = debit_micros + 1 WHERE debit_micros > 0"))
    with session_scope(engine, write=False) as db:
        assert balance(db, wid, "platform:rounding") == 7


def test_unconfirmed_provider_operations_cannot_move_money(book):
    engine, _ = book
    with session_scope(engine) as db:
        for op, row in ((record_capture, Capture(status="PENDING")),
                        (record_refund, Refund(status="PENDING")),
                        (record_payout, PayoutItem(status="PENDING"))):
            with pytest.raises(LedgerError):
                op(db, row)
        assert db.scalar(select(func.count(LedgerEntry.id))) == 0


def test_split_principal_preserves_royalties_and_sla_portion():
    full = Parts(4933, 3945, {"L1": 480, "L2": 560})
    first = full.portion(3000)
    rest = full.minus(first)
    assert first.principal + rest.principal == 4933
    assert first.public + rest.public == 3945
    assert first.royalties["L1"] + rest.royalties["L1"] == 480
    assert first.royalties["L2"] + rest.royalties["L2"] == 560
    assert rest.for_reason("sla").principal == rest.public
    assert rest.for_reason("dispute").principal == rest.principal


def test_refund_rounding_is_cumulative_across_separately_requested_calls():
    # The provider has a one-cent minimum per actual payment; carry rounding
    # forward so splitting a dispute cannot pay one cent for every microdollar.
    assert refund_cents(5, 0, 0, 1000) == 1
    assert refund_cents(5, 1, 1000, 1000) == 0
    assert refund_cents(5, 1, 2000, 13000) == 1
    assert refund_cents(5, 4, 40000, 12000) == 1
    assert refund_cents(5, 5, 52000, 10) == 0
