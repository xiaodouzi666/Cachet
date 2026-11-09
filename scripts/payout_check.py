"""Verify funded, whole-cent publisher payouts using real GPU recordings and PayPal Sandbox."""

import json
import os
import sys
import time
from pathlib import Path

from fastapi import BackgroundTasks
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app import ledger
from backend.app.api.agent import persist, replay_turns
from backend.app.api.catalog import PublishInput, PublisherInput, publish, publish_owner
from backend.app.config import calibration
from backend.app.db import session_scope
from backend.app.gateway.billing import Rates
from backend.app.models import AgentRun, Call, Capture, Mandate, PayoutBatch, PayoutItem, utcnow
from backend.app.paypal.client import PayPalClient, require, settings
from backend.app.paypal.reconciliation import reconcile
from backend.app.replay.recordings import load
from backend.app.settlement import service
from backend.app.settlement.audit import audit
from backend.app.settlement.payout_job import settle
from scripts.mandate_check import workspace
from scripts.sandbox_smoke import check, locked_journal


def balances(ws, owners, *, funded):
    with session_scope(write=False) as db:
        if funded:
            return {s: ledger.available_payable(db, ws, p) for s, p in owners.items()}
        return {s: -ledger.balance(db, ws, ledger.payable(p)) for s, p in owners.items()}


def book_segment(ws, mid, owners, contexts, track, number):
    name = f"Publisher payout acceptance, replay segment {number}"
    with session_scope() as db:
        run = db.scalar(select(AgentRun).where(AgentRun.mandate_id == mid, AgentRun.task_text == name))
        if run is None:
            run = AgentRun(workspace_id=ws, mandate_id=mid, task_id=track["task_id"], task_text=name,
                           quote_json={"chain": list(owners), "context_ids": contexts},
                           status="running", started_at=utcnow(), mode="replay")
            db.add(run)
            db.flush()
        if run.status == "completed":
            return
        rid = run.id
        done = set(db.scalars(select(Call.turn).where(Call.run_id == rid)))
    rates, profile = Rates.from_settings(settings()), calibration()
    for turn in replay_turns(track, owners, lambda: 0):
        if turn["kind"] == "result" and turn["turn"] not in done:
            persist(ws, rid, mid, owners, turn, profile, rates)
    audit(ws, mid)
    with session_scope() as db:
        run = db.get(AgentRun, rid)
        run.status, run.ended_at = "completed", utcnow()


def generate_royalties(journal, ws, mid, owners, contexts):
    track = load("rate-limit", True)
    for number in range(1, 41):
        book_segment(ws, mid, owners, contexts, track, number)
        if "below_minimum" not in journal.data["readbacks"]:
            service.capture(ws, mid, force=True)
            first = balances(ws, owners, funded=True)
            check(all(0 < x < 10000 for x in first.values()), "Both publishers must have earned funded sub-cent royalties.")
            check(settle(ws)["status"] == "below_minimum", "A sub-cent balance triggered a payment.")
            journal.record("below_minimum", {"funded_micros": first})
        if min(balances(ws, owners, funded=False).values()) >= 10000:
            service.close(ws, mid)
            return
    raise RuntimeError("The measured replay did not earn a whole cent for both publishers in forty segments.")


def main():
    os.umask(0o077)
    directory = ROOT / "logs" / "payout-acceptance"
    os.environ["DATABASE_URL"] = "sqlite:///" + str(directory / "ledger.sqlite")
    cfg = settings()
    with locked_journal(directory / "journal.json", "publisher-payout-acceptance") as journal:
        ws = workspace()
        owners, contexts, recipients = {}, [], {}
        for slug in ("starlette", "fastapi"):
            email = require(cfg, "PUBLISHER_" + slug.upper() + "_EMAIL")
            owner = publish_owner(PublisherInput(name=slug.capitalize() + " maintainer", paypal_email=email), ws)
            # Register the licensed context. Replay uses archived measurements and needs no live warm probe.
            source = publish(PublishInput(publisher_id=owner["id"], source_slug=slug,
                             parent_id=contexts[-1] if contexts else None), BackgroundTasks(), ws)
            owners[slug] = owner["id"]
            contexts.append(source["id"])
            recipients[slug] = email.casefold()
        check(len(set(recipients.values())) == 2, "The test requires distinct configured sandbox recipients.")
        with session_scope(write=False) as db:
            mandate = db.scalars(select(Mandate)).one_or_none()
        if mandate is None or not mandate.authorization_id:
            opened = service.open_order(ws, 5_000_000, card=True)
            mid = service.authorize(ws, opened["id"])["id"]
        else:
            mid = mandate.id
        if mandate is None or mandate.status not in {"CLOSED", "CLOSING"}:
            generate_royalties(journal, ws, mid, owners, contexts)
        closed = service.close(ws, mid)
        check(closed["status"] == "CLOSED" and closed["captured_micros"] > 0, "The accrued royalties require a real capture.")
        if "eligible" not in journal.data["readbacks"]:
            amounts = balances(ws, owners, funded=True)
            check(min(amounts.values()) >= 10000, "Both publishers must qualify for a whole-cent payout.")
            journal.record("eligible", {"funded_micros": amounts})
        before = journal.data["readbacks"]["eligible"]["funded_micros"]
        with session_scope(write=False) as db:
            batch = db.scalars(select(PayoutBatch)).one_or_none()
        response = settle(ws, batch.id if batch else None)
        deadline = time.monotonic() + 180
        while True:
            with session_scope(write=False) as db:
                items = list(db.scalars(select(PayoutItem).where(PayoutItem.batch_id == response["id"])))
            if response["status"] == "SUCCESS" and len(items) == 2 and all(i.status == "SUCCESS" for i in items):
                break
            check(time.monotonic() < deadline, "Payout incomplete; resume this journal before starting another.")
            time.sleep(2)
            response = settle(ws, response["id"])
        matching = {owner: slug for slug, owner in owners.items()}
        verified_items = []
        with PayPalClient.from_env() as pp:
            hdr = pp.get_payout(response["paypal_batch_id"])["batch_header"]
            for item in items:
                slug = matching[item.publisher_id]
                remote = pp.get_payout_item(item.paypal_item_id)
                check(remote["transaction_status"] == "SUCCESS" and remote["payout_item"]["receiver"].casefold() == recipients[slug]
                      and remote["payout_item"]["sender_item_id"] == item.id and remote["payout_batch_id"] == response["paypal_batch_id"]
                      and service.cents(remote["payout_item"]["amount"]) == item.amount_cents == before[slug] // 10000
                      and service.cents(remote["payout_item_fee"]) == item.fee_cents, "Publisher item GET did not match its earned payment.")
                verified_items.append({"publisher": slug, "id": item.paypal_item_id, "cents": item.amount_cents,
                                       "fee_cents": item.fee_cents, "status": remote["transaction_status"]})
        check(settle(ws, response["id"]) == response, "Repeating the exact batch changed its receipt.")
        check(settle(ws)["status"] == "below_minimum", "Repeating settlement reserved a new payout.")
        after = balances(ws, owners, funded=True)
        check(after == {slug: value % 10000 for slug, value in before.items()}, "Fractional royalties did not carry forward.")
        with session_scope(write=False) as db:
            check(db.scalar(select(func.count(PayoutBatch.id))) == 1 and db.scalar(select(func.count(PayoutItem.id))) == 2,
                  "Repeated settlement created additional payout records.")
            accruals = db.scalar(select(func.count(Call.id)))
            captures = list(db.scalars(select(Capture.paypal_capture_id)))
        reconciliation = reconcile(ws)
        check(reconciliation["ok"] is True, "Payout sandbox reconciliation did not match.")
        result = {"workspace_id": ws, "mandate_id": mid, "order_id": closed["paypal_order_id"],
                  "measured_replay_accruals": accruals, "captures": captures,
                  "funded_below_minimum": journal.data["readbacks"]["below_minimum"]["funded_micros"],
                  "funded_before_micros": before, "carried_micros": after, "paypal_batch_id": response["paypal_batch_id"],
                  "sender_batch_id": hdr["sender_batch_header"]["sender_batch_id"], "batch_status": hdr["batch_status"],
                  "batch_fee_cents": service.cents(hdr["fees"]), "items": verified_items,
                  "batch_count_after_two_retries": 1, "reconciliation": reconciliation}
        journal.record("verified", result)
        journal.data["completed"] = True
        journal.save()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
