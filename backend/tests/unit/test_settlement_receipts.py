from uuid import uuid4

from sqlalchemy.orm import Session

from backend.app.api.finance import paypal_events, system_prefix
from backend.app.db import init_db
from backend.app.models import AgentRun, Call, Consumer, Mandate, WebhookEvent, Workspace


def test_settlement_views_use_the_requested_workspace(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/receipts.db"
    monkeypatch.setenv("DATABASE_URL", url)
    scopes = []
    with Session(init_db(url)) as db, db.begin():
        for system_hits in (3, 7):
            ws = Workspace(cookie_id=uuid4().hex, kind="judge")
            db.add(ws)
            db.flush()
            buyer = Consumer(workspace_id=ws.id, name="Test buyer", salt_hex=uuid4().hex[:8])
            db.add(buyer)
            db.flush()
            mandate = Mandate(workspace_id=ws.id, consumer_id=buyer.id, cap_micros=1000000,
                              paypal_order_id=uuid4().hex, authorization_id=uuid4().hex)
            db.add(mandate)
            db.flush()
            run = AgentRun(workspace_id=ws.id, mandate_id=mandate.id, task_id="isolated", task_text="Unit test")
            db.add(run)
            db.flush()
            db.add(Call(workspace_id=ws.id, run_id=run.id, turn=0, prompt_tokens=20,
                        cached_tokens=system_hits, spans_json=[], hits_json={"system": system_hits},
                        input_hash="unit", output_text="", output_tokens=0, ttft_ms=0, total_ms=0,
                        charge_micros=0, public_charge_micros=0, royalty_json={}, platform_micros=0, mode="replay"))
            for typ, rid, verified in (
                ("CHECKOUT.ORDER.APPROVED", mandate.paypal_order_id, True),
                ("PAYMENT.AUTHORIZATION.CREATED", mandate.authorization_id, True),
                ("CHECKOUT.ORDER.APPROVED", mandate.paypal_order_id, False),
                ("PAYMENT.CAPTURE.COMPLETED", mandate.paypal_order_id, True),
            ):
                db.add(WebhookEvent(paypal_event_id=uuid4().hex, resource_id=rid, event_type=typ,
                                    verified=verified, raw_json={"private": "excluded"}))
            scopes.append((ws.id, mandate.paypal_order_id, mandate.authorization_id, system_hits))
    for wid, order, authorization, expected in scopes:
        events = paypal_events(wid)
        assert {(row["event_type"], row["resource_id"]) for row in events} == {
            ("CHECKOUT.ORDER.APPROVED", order), ("PAYMENT.AUTHORIZATION.CREATED", authorization),
        }
        assert len(events) == 2
        assert all(set(row) == {"event_type", "resource_id", "verified", "received_at"} for row in events)
        assert system_prefix(wid)["hit_tokens"] == expected
    assert system_prefix(uuid4().hex)["hit_tokens"] == 0
