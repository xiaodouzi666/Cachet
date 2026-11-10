"""Verify fee-aware close decisions using real archived metering and isolated PayPal sandbox ledgers."""

import json
import os
import secrets
import sys
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


def replay_segment(ws, mid, number):
    """Book one checksum-verified private-compute track with the production meter and ledger."""
    track = load("rate-limit", False)
    task = track["task_id"]
    profile, rates = calibration(), Rates.from_settings(settings())
    name = f"Fee-aware settlement recording {number}"
    with session_scope() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.mandate_id == mid, AgentRun.task_text == name))
        if run is None:
            run = AgentRun(workspace_id=ws, mandate_id=mid, task_id=task, task_text=name,
                           quote_json={"chain": []}, status="running", started_at=utcnow(), mode="replay")
            db.add(run)
            db.flush()
        if run.status == "completed":
            return
        rid = run.id
    for meter in track["turns"]:
        with session_scope(write=False) as db:
            existing = db.scalar(select(Call).where(Call.run_id == rid, Call.turn == meter["turn"]))
        if existing is None:
            persist(ws, rid, mid, {}, {**meter, "mode": "replay"}, profile, rates)
        else:
            check(existing.input_hash == meter["input_hash"], "Recorded inputs changed during resume.")
        with session_scope(write=False) as db:
            m = db.get(Mandate, mid)
            due = m.status == "CAPTURING" or m.accrued_uncaptured_micros >= service.capture_threshold()
        if due:
            service.capture(ws, mid)
    with session_scope() as db:
        run = db.get(AgentRun, rid)
        run.status, run.ended_at = "completed", utcnow()


def confirm(ws, mid, scenario):
    closed = service.close(ws, mid)
    check(closed == service.close(ws, mid), "Repeated close changed the mandate.")
    with session_scope(write=False) as db:
        count, accrued = db.execute(select(func.count(Call.id), func.sum(Call.charge_micros))).one()
        caps = list(db.scalars(select(Capture).where(Capture.mandate_id == mid).order_by(Capture.created_at)))
    check(not closed["accrued_uncaptured_micros"], "The closed consumer still owes an accrual.")
    if scenario == "final":
        check(len(caps) >= 2 and caps[-1].final and all(not c.final for c in caps[:-1]),
              "Expected a threshold capture followed by a final capture.")
        check(closed["captured_micros"] == round_cents(accrued) * 10000 and not closed["forgiven_micros"],
              "Final capture differs from the actual metered charges.")
    elif scenario == "partial_then_forgive":
        check(len(caps) == 1 and not caps[0].final and 0 < closed["forgiven_micros"] < service.final_minimum() and
              accrued == closed["captured_micros"] + closed["forgiven_micros"], "Subminimum charges were not forgiven.")
    else:
        check(count == 0 and not caps and not closed["forgiven_micros"], "Unused authorization has a charge.")
    with PayPalClient.from_env() as pp:
        auth = pp.get_authorization(closed["authorization_id"])
        check(auth["id"] == closed["authorization_id"] and auth["status"] == ("CAPTURED" if scenario == "final" else "VOIDED"),
              "Authorization disposition differs from the close decision.")
        charges = []
        for c in caps:
            remote = pp.get_capture(c.paypal_capture_id)
            check(remote["status"] == "COMPLETED" and capture_flag_matches(c.final, remote["final_capture"], auth["status"]) and
                  service.cents(remote["amount"]) == c.amount_cents, "PayPal capture mismatch.")
            net = service.cents(remote["seller_receivable_breakdown"]["net_amount"])
            check(net == c.amount_cents - c.fee_cents, "Capture amount, fee and net do not match PayPal.")
            charges.append({"id": remote["id"], "cents": c.amount_cents, "fee_cents": c.fee_cents, "net_cents": net,
                            "requested_final": c.final, "provider_final_at_close": remote["final_capture"]})
        released = service.cents(auth["amount"]) - sum(c["cents"] for c in charges)
    check(released * 10000 == closed["released_micros"] and closed["status"] == ("VOIDED" if scenario == "unused" else "CLOSED"),
          "Uncaptured budget or terminal mandate state differs.")
    from backend.app.api.finance import captures as list_captures
    public = {row["paypal_capture_id"]: row for row in list_captures(ws)}
    for charge in charges:
        row = public[charge["id"]]
        check((row["amount_cents"], row["fee_cents"], row["net_cents"]) ==
              (charge["cents"], charge["fee_cents"], charge["net_cents"]), "Capture listing differs from PayPal GET.")
    baseline = reconcile(ws)
    check(baseline["ok"] is True and baseline["checked"] == len(caps) + 2, "Real ledger reconciliation failed.")
    return {"workspace_id": ws, "mandate_id": mid, "order_id": closed["paypal_order_id"], "authorization_id": auth["id"],
            "authorization_status": auth["status"], "source": "metering simulation from recorded GPU compute tracks",
            "replayed_accruals": count, "accrued_micros": accrued or 0, "captured_micros": closed["captured_micros"],
            "released_micros": closed["released_micros"], "captures": charges, "reconciliation": baseline,
            "forgiven_micros": closed["forgiven_micros"], "repeat_close_unchanged": True}


def check_case(directory, scenario):
    database = (directory / scenario / "ledger.sqlite").resolve()
    os.environ["DATABASE_URL"] = "sqlite:///" + str(database)
    with locked_journal(database.parent / "journal.json", "fee-aware-" + scenario) as journal:
        ws = workspace()
        with session_scope(write=False) as db:
            mandate = db.scalars(select(Mandate)).one_or_none()
        if mandate is None or not mandate.authorization_id:
            opened = service.open_order(ws, 5_000_000, card=True)
            mid = service.authorize(ws, opened["id"])["id"]
        else:
            mid = mandate.id
        with session_scope(write=False) as db:
            active = db.get(Mandate, mid).status in {"ACTIVE", "CAPTURING"}
        if active:
            if scenario == "partial_then_forgive":
                replay_segment(ws, mid, 1)
                with session_scope(write=False) as db:
                    preview = service.capture_preview(db.get(Mandate, mid))
                    captured = db.scalar(select(Capture.id).where(Capture.mandate_id == mid))
                if not captured:
                    from backend.app.api.finance import MandateAction, capture_now
                    receipt = capture_now(MandateAction(mandate_id=mid), ws)
                    check(receipt["amount_cents"] == preview["amount_cents"] and preview["below_threshold"],
                          "Capture now did not honor the actual preview.")
                    journal.record("judge_capture", {"preview": preview, "actual": receipt})
                replay_segment(ws, mid, 2)
            if scenario == "final":
                for number in range(1, 151):
                    with session_scope(write=False) as db:
                        m = db.get(Mandate, mid)
                        if m.captured_micros and m.accrued_uncaptured_micros >= service.final_minimum():
                            break
                    replay_segment(ws, mid, number)
                else:
                    raise RuntimeError("The measured workload did not reach both capture thresholds.")
        result = confirm(ws, mid, scenario)
        if "judge_capture" in journal.data["readbacks"]:
            result["judge_capture"] = journal.data["readbacks"]["judge_capture"]
        journal.record("settlement", result)
        journal.data["completed"] = True
        journal.save()
        return result


def main():
    os.umask(0o077)
    directory = ROOT / "logs" / "fee-aware-acceptance"
    print(json.dumps({case: check_case(directory, case) for case in ("unused", "partial_then_forgive", "final")}, indent=2))


if __name__ == "__main__":
    main()
