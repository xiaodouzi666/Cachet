"""Verify thirty replay accruals, real sandbox captures, release, and reconciliation on an isolated ledger."""

import json
import os
import secrets
import sqlite3
import sys
import tempfile
from pathlib import Path

from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.api.agent import persist
from backend.app.config import calibration
from backend.app.db import session_scope
from backend.app.gateway.billing import Rates
from backend.app.models import AgentRun, Call, Capture, Consumer, Mandate, Workspace, utcnow
from backend.app.money import round_cents
from backend.app.paypal.client import PayPalClient, capture_flag_matches, settings
from backend.app.paypal.reconciliation import reconcile
from backend.app.replay.recordings import load
from backend.app.settlement import service
from scripts.sandbox_smoke import check, locked_journal


def workspace():
    with session_scope() as db:
        ws = db.scalars(select(Workspace)).one_or_none()
        if ws is None:
            ws = Workspace(kind="judge", cookie_id=secrets.token_hex(32))
            db.add(ws)
            db.flush()
            db.add(Consumer(workspace_id=ws.id, name="Mandate acceptance", salt_hex=secrets.token_hex(16)))
        return ws.id


def replay_calls(ws, mid):
    profile = calibration()
    rates = Rates.from_settings(settings())
    # Use actual private-compute recordings so the isolated ledger has no royalties to disburse.
    for number, task in enumerate(("rate-limit", "route-class", "rate-limit"), 1):
        with session_scope() as db:
            name = f"Mandate acceptance, replay segment {number}"
            run = db.scalar(select(AgentRun).where(AgentRun.mandate_id == mid, AgentRun.task_text == name))
            if run is None:
                run = AgentRun(workspace_id=ws, mandate_id=mid, task_id=task, task_text=name,
                               quote_json={"chain": []}, status="running", started_at=utcnow(), mode="replay")
                db.add(run)
                db.flush()
            if run.status == "completed":
                continue
            rid = run.id
        track = load(task, False)
        for meter in track["turns"]:
            with session_scope(write=False) as db:
                existing = db.scalar(select(Call).where(Call.run_id == rid, Call.turn == meter["turn"]))
            if existing is None:
                persist(ws, rid, mid, {}, {**meter, "mode": "replay"}, profile, rates)
            else:
                check(existing.input_hash == meter["input_hash"], "Recorded inputs changed during resume.")
            # Exercise the normal periodic threshold through the first two segments.
            # The final ten calls accrue until close, exercising a true final capture.
            if number < 3:
                service.capture(ws, mid)
        with session_scope() as db:
            run = db.get(AgentRun, rid)
            run.status = "completed"
            run.ended_at = run.ended_at or utcnow()


def confirm(ws, mid, database):
    closed = service.close(ws, mid)
    check(closed == service.close(ws, mid), "Repeated close changed the mandate.")
    with session_scope(write=False) as db:
        count, accrued = db.execute(select(func.count(Call.id), func.sum(Call.charge_micros))).one()
        caps = list(db.scalars(select(Capture).where(Capture.mandate_id == mid).order_by(Capture.created_at)))
    check(count == 30 and len(caps) >= 2 and caps[-1].final and not any(c.final for c in caps[:-1]),
          "Expected thirty accrued calls plus partial and final captures.")
    check(closed["captured_micros"] == round_cents(accrued) * 10000 and not closed["accrued_uncaptured_micros"],
          "Closed balance differs from the thirty metered accruals.")
    with PayPalClient.from_env() as pp:
        auth = pp.get_authorization(closed["authorization_id"])
        check(auth["id"] == closed["authorization_id"] and auth["status"] == "CAPTURED", "Final authorization did not settle.")
        charges = []
        for c in caps:
            remote = pp.get_capture(c.paypal_capture_id)
            check(remote["status"] == "COMPLETED" and capture_flag_matches(c.final, remote["final_capture"], auth["status"]) and
                  service.cents(remote["amount"]) == c.amount_cents, "PayPal capture mismatch.")
            charges.append({"id": remote["id"], "cents": c.amount_cents, "fee_cents": c.fee_cents,
                            "requested_final": c.final, "provider_final_at_close": remote["final_capture"]})
        released = service.cents(auth["amount"]) - sum(c["cents"] for c in charges)
    check(released * 10000 == closed["released_micros"] and closed["status"] == "CLOSED", "Uncaptured budget differs.")
    baseline = reconcile(ws)
    check(baseline["ok"] is True and baseline["checked"] == len(caps) + 2, "Real ledger reconciliation failed.")
    # Corrupt only a disposable copy, then compare with the same signed-in provider.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.sqlite"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as original, sqlite3.connect(copy) as snapshot:
            original.backup(snapshot)
            snapshot.execute("UPDATE capture SET fee_cents = fee_cents + 1 WHERE id = ?", (caps[0].id,))
        old = os.environ["DATABASE_URL"]
        try:
            os.environ["DATABASE_URL"] = "sqlite:///" + str(copy)
            tampered = reconcile(ws)
        finally:
            os.environ["DATABASE_URL"] = old
    check(tampered["ok"] is False and any(d.get("id") == caps[0].id and d.get("field") == "capture fee"
                                         for d in tampered["differences"]), "Reconciliation missed the snapshot edit.")
    return {"workspace_id": ws, "mandate_id": mid, "order_id": closed["paypal_order_id"], "authorization_id": auth["id"],
            "authorization_status": auth["status"], "source": "metering simulation from recorded GPU compute tracks",
            "replayed_accruals": count, "accrued_micros": accrued, "captured_micros": closed["captured_micros"],
            "released_micros": closed["released_micros"], "captures": charges, "reconciliation": baseline,
            "snapshot_differences": tampered["differences"], "repeat_close_unchanged": True}


def main():
    os.umask(0o077)
    directory = ROOT / "logs" / "mandate-acceptance"
    database = directory / "ledger.sqlite"
    os.environ["DATABASE_URL"] = "sqlite:///" + str(database)
    with locked_journal(directory / "journal.json", "mandate-30-accruals") as journal:
        ws = workspace()
        with session_scope(write=False) as db:
            m = db.scalars(select(Mandate)).one_or_none()
        if m is None or not m.authorization_id:
            m = service.open_order(ws, 5_000_000, card=True)
            m = service.authorize(ws, m["id"])
            mid = m["id"]
        else:
            mid = m.id
        with session_scope(write=False) as db:
            state = db.get(Mandate, mid).status
        if state in {"ACTIVE", "CAPTURING"}:
            replay_calls(ws, mid)
        result = confirm(ws, mid, database)
        journal.record("settlement", result)
        journal.data["completed"] = True
        journal.save()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
