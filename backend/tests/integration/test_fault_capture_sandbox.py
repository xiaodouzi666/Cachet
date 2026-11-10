"""Exercise three delayed replay turns against an actual sandbox card, capture, refund and release."""


from fastapi import BackgroundTasks
import pytest
from sqlalchemy import func, select

from backend.app.api.agent import persist, replay_turns
from backend.app.api.catalog import PublishInput, PublisherInput, publish, publish_owner
from backend.app.api.finance import FaultCaptureRequest, collect_fault_turns
from backend.app.config import ROOT, calibration
from backend.app.db import session_scope
from backend.app.gateway.billing import Rates
from backend.app.money import allocate
from backend.app.models import AgentRun, Call, Capture, CaptureAllocation, Mandate, Refund, utcnow
from backend.app.operations import Control
from backend.app.paypal.client import PayPalClient, require, settings
from backend.app.paypal.reconciliation import reconcile
from backend.app.replay.recordings import load
from backend.app.settlement import service
from backend.app.settlement.audit import audit
from scripts.mandate_check import workspace
from scripts.sandbox_smoke import locked_journal


@pytest.mark.sandbox
def test_three_real_fault_allocations_and_repeat_capture(monkeypatch):
    directory = ROOT / "logs" / "judge-fault-capture"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(directory / "ledger.sqlite"))
    cfg = settings()
    with locked_journal(directory / "journal.json", "three-delayed-turns") as journal:
        ws = workspace()
        owners, contexts = {}, []
        for slug in ("starlette", "fastapi"):
            p = publish_owner(PublisherInput(name=slug, paypal_email=require(cfg, "PUBLISHER_" + slug.upper() + "_EMAIL")), ws)
            layer = publish(PublishInput(source_slug=slug, publisher_id=p["id"], parent_id=contexts[-1] if contexts else None),
                            BackgroundTasks(), ws)
            owners[slug] = p["id"]
            contexts.append(layer["id"])
        with session_scope(write=False) as db:
            m = db.scalars(select(Mandate)).one_or_none()
        if m is None or not m.authorization_id:
            m = service.open_order(ws, 1_000_000, card=True)
            mid = service.authorize(ws, m["id"])["id"]
        else:
            mid = m.id
        recording = load("rate-limit", True)
        for count, delay in ((1, 0), (3, 1200)):
            with session_scope() as db:
                name = f"Targeted capture acceptance with {count} recorded turns"
                run = db.scalar(select(AgentRun).where(AgentRun.mandate_id == mid, AgentRun.task_text == name))
                if not run:
                    run = AgentRun(workspace_id=ws, mandate_id=mid, task_id="rate-limit", task_text=name, status="running",
                                   mode="replay", quote_json={"chain":list(owners), "context_ids":contexts}, started_at=utcnow())
                    db.add(run)
                    db.flush()
                    if delay:
                        db.add(Control(key="judge-fault-run:" + run.id, data={"workspace_id":ws,"mandate_id":mid}))
                rid = run.id
                done = set(db.scalars(select(Call.turn).where(Call.run_id == rid)))
            if len(done) < count:
                for turn in replay_turns({**recording, "turns":recording["turns"][:count]}, owners, lambda: delay):
                    if turn["kind"] == "result" and turn["turn"] not in done:
                        persist(ws, rid, mid, owners, turn, calibration(), Rates.from_settings(cfg))
                with session_scope() as db:
                    run = db.get(AgentRun, rid)
                    run.status, run.ended_at = "completed", utcnow()
        body = FaultCaptureRequest(mandate_id=mid, run_id=rid)
        first = collect_fault_turns(body, ws)
        receipt = collect_fault_turns(body, ws)
        assert first == receipt and len(receipt["captures"]) == 1 and len(receipt["target_call_ids"]) == 3
        assert {a["call_id"] for a in receipt["allocations"]} == set(receipt["target_call_ids"])
        with session_scope(write=False) as db:
            allocations = list(db.scalars(select(CaptureAllocation).where(CaptureAllocation.capture_id == receipt["id"])))
            assert {a.call_id for a in allocations} == set(receipt["target_call_ids"])
            assert all(a.public_micros > 0 for a in allocations)
            weights = [db.get(Call, call_id).charge_micros for call_id in receipt["target_call_ids"]]
            shares = allocate(receipt["amount_cents"] * 10000, weights)
            assert {a.call_id:a.principal_micros for a in allocations} == dict(zip(receipt["target_call_ids"], shares))
            assert sum(a.principal_micros for a in allocations) == receipt["amount_cents"] * 10000
        result = audit(ws, mid, run_id=rid)
        closed = service.close(ws, mid)
        assert len(result["refunds"]) == 1 and closed["status"] == "CLOSED"
        with PayPalClient.from_env() as pp:
            refund = pp.get_refund(result["refunds"][0]["paypal_refund_id"])
            auth = pp.get_authorization(closed["authorization_id"])
            assert refund["status"] == "COMPLETED" and auth["status"] == "VOIDED"
        observed = collect_fault_turns(body, ws)
        assert [c["paypal_capture_id"] for c in observed["captures"]] == [receipt["paypal_capture_id"]]
        with session_scope(write=False) as db:
            assert db.scalar(select(func.count(Capture.id))) == db.scalar(select(func.count(Refund.id))) == 1
        report = reconcile(ws)
        assert report["ok"] is True
        evidence = {"mandate_id":mid, "run_id":rid, "source":"three delayed replayed GPU turns plus an earlier, unrelated call",
                    "capture":receipt, "allocations":[{"call_id":a.call_id,"principal_micros":a.principal_micros,
                    "public_micros":a.public_micros} for a in allocations], "refund_id":refund["id"],
                    "refund_amount":refund["amount"],"reconciliation":report}
        journal.record("verified", evidence)
        journal.data["completed"] = True
        journal.save()
