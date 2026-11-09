"""Public/isolated catalogs and canonical cross-workspace journals on real SQLite."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app import ledger, marketplace
from backend.app.api.catalog import chain, view
from backend.app.config import calibration, manifest, sources
from backend.app.db import engine_for, init_db, session_scope
from backend.app.gateway.billing import Span, price_call
from backend.app.models import Context, LedgerEntry, Publisher, Workspace
from backend.app.replay.recordings import load


def workspace(db, kind):
    ws = Workspace(cookie_id=uuid4().hex, kind=kind)
    db.add(ws)
    db.flush()
    return ws


def layer(db, ws, slug, parent=None):
    spec = sources()[slug]
    project = manifest()["projects"][slug]
    # No test funds can be routed: these throwaway database publishers have no receiver address.
    owner = Publisher(workspace_id=ws.id, name=slug, paypal_receiver_email="")
    db.add(owner)
    db.flush()
    ctx = Context(
        workspace_id=ws.id,
        publisher_id=owner.id,
        slug=slug,
        title=slug,
        parent_id=parent.id if parent and parent.workspace_id == ws.id else None,
        source_url=project["source_url"],
        version=project["revision"],
        license=project["license"],
        tokenizer=manifest()["tokenizer"]["revision"],
        text_path=spec["text_path"],
        token_len=spec["token_len"],
        token_hash=spec["ids_sha256"],
    )
    db.add(ctx)
    db.flush()
    marketplace.publish(db, ctx, parent)
    return ctx


def test_public_chain_and_judge_privacy(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path}/catalog.db")
    with session_scope(engine) as db:
        a, b, buyer, judge = [
            workspace(db, kind) for kind in ("user", "user", "user", "judge")
        ]
        base = layer(db, a, "starlette")
        leaf = layer(db, b, "fastapi", base)
        private = layer(db, judge, "starlette")
        assert [
            c.id for c in chain(db, marketplace.visible_context(db, leaf.id, buyer.id))
        ] == [base.id, leaf.id]
        for viewer in [None, buyer.id]:
            assert {
                c.id for c in db.scalars(marketplace.visible_contexts(db, viewer))
            } == {base.id, leaf.id}
            projection = view(db, leaf, viewer)
            assert not projection["can_manage"] and projection["parent_id"] == base.id
            assert (
                not {"workspace_id", "paypal_receiver_email", "call_ids"}
                & projection.keys()
            )
        assert view(db, base, a.id)["can_manage"]
        assert [
            c.id for c in db.scalars(marketplace.visible_contexts(db, judge.id))
        ] == [private.id]
        for identifier, viewer in (
            (private.id, buyer.id),
            (private.id, None),
            (base.id, judge.id),
        ):
            with pytest.raises(HTTPException) as error:
                marketplace.visible_context(db, identifier, viewer)
            assert error.value.status_code == 404


def test_real_meter_net_royalty_and_reversals_follow_canonical_payee(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path}/journals.db")
    with session_scope(engine) as db:
        buyer, seller, judge = [
            workspace(db, kind) for kind in ("user", "user", "judge")
        ]
        context = layer(db, seller, "starlette")
        receiver = ledger.payable(context.publisher_id)
        metered = load("route-class", True)["turns"][0]
        fit = calibration()["fit"]
        bill = price_call(
            spans=[Span(**s) for s in metered["spans"]],
            cached_tokens=metered["cached_tokens"],
            output_tokens=metered["output_tokens"],
            alpha=fit["alpha"],
            beta=fit["beta"],
        )
        share = bill.royalty_micros["starlette"]
        assert share > 0
        # A local accounting vector: use a revenue offset to isolate royalty routing.
        # There is no buyer obligation and no PayPal order, capture, refund or payout.
        ref = uuid4().hex
        delta = {ledger.REVENUE: share, receiver: -share}
        assert ledger.post(db, buyer.id, "call", ref, delta)
        assert not ledger.post(db, buyer.id, "call", ref, delta)
        assert ledger.balance(db, seller.id, receiver) == -share
        assert ledger.available_payable(db, seller.id, context.publisher_id) == 0
        assert ledger.balance(db, buyer.id, receiver) == 0
        assert ledger.balance(db, buyer.id, ledger.INTERWORKSPACE) == -share
        entries = list(
            db.scalars(select(LedgerEntry).where(LedgerEntry.workspace_id == seller.id))
        )
        assert all(e.ref_id != ref and buyer.id not in e.account for e in entries)
        assert ledger.post(
            db, buyer.id, "uncaptured_dispute", ref, {k: -v for k, v in delta.items()}
        )
        assert ledger.available_payable(db, seller.id, context.publisher_id) == 0
        assert ledger.assert_invariants(db, expected_clearing_micros=0) == 0
        with pytest.raises(ledger.LedgerError, match="cross-workspace"):
            ledger.post(db, judge.id, "call", uuid4().hex, delta)


def test_upgrade_preserves_private_existing_contexts(tmp_path):
    url = f"sqlite:///{tmp_path}/pre-market.db"
    engine = engine_for(url)
    from backend.migrations import v001_initial

    with engine.begin() as connection:
        v001_initial.upgrade(connection)
        connection.exec_driver_sql("DROP TABLE royalty_usage")
        connection.exec_driver_sql("DROP TABLE market_listing")
        connection.exec_driver_sql("PRAGMA user_version=1")
    with session_scope(engine) as db:
        judge = workspace(db, "judge")
        private = layer(db, judge, "starlette")
        call, mandate, entry = archived_call(db, judge, private)
        from backend.app.operations import Control, Operation, RunEvent

        draft = Operation(
            id=uuid4().hex,
            workspace_id=judge.id,
            kind="order",
            subject=mandate.id,
            payload={"budget_micros": mandate.cap_micros},
        )
        marker = Control(
            key="historical-run:" + call.run_id, data={"status": "reviewed"}
        )
        event = RunEvent(
            run_id=call.run_id, event_type="completed", payload={"mode": "replay"}
        )
        db.add_all([draft, marker, event])
        # v1 also supports native spans without denormalized layer totals.
        call.spans_json = [
            {k: v for k, v in span.items() if k in ("kind", "owner", "start", "end")}
            for span in call.spans_json
        ]
        call.linear_royalty_json = {private.publisher_id: entry["linear_micros"]}
        legacy, _, _ = archived_call(db, judge, private)
        # Another v1 call stored aggregate gross royalties but no linear alternative.
        # Original accounting survives even when that comparison cannot be reconstructed.
        legacy.spans_json = [
            {k: v for k, v in span.items() if k in ("kind", "owner", "start", "end")}
            for span in legacy.spans_json
        ]
        legacy.linear_royalty_json = {}
    with session_scope(init_db(url), write=False) as db:
        assert marketplace.totals(db, private.id) == (
            entry["hits"],
            entry["royalty_micros"],
            entry["linear_micros"],
        )
        assert not marketplace.complete_history(db, private.id)
        from backend.app.models import Call

        assert db.get(Call, legacy.id).royalty_json == legacy.royalty_json
        assert db.get(Operation, draft.id).external_id is None
        assert db.get(Control, marker.key).data == marker.data
        assert db.get(RunEvent, event.id).payload == event.payload
        assert (
            db.get(Context, private.id).token_hash
            == sources()["starlette"]["ids_sha256"]
        )
        assert not list(db.scalars(marketplace.visible_contexts(db, None)))
        assert db.connection().exec_driver_sql("PRAGMA foreign_key_check").all() == []


def archived_call(db, buyer, source):
    from backend.app.models import AgentRun, Call, Consumer, Mandate

    consumer = Consumer(
        workspace_id=buyer.id, salt_hex=uuid4().hex[:8], name="Usage projection"
    )
    db.add(consumer)
    db.flush()
    # A local draft anchors the archived reading. No authorization or charge is submitted.
    mandate = Mandate(workspace_id=buyer.id, consumer_id=consumer.id, cap_micros=10000)
    db.add(mandate)
    db.flush()
    run = AgentRun(
        workspace_id=buyer.id,
        mandate_id=mandate.id,
        task_id="route-class",
        task_text="Inspect recorded public usage",
        mode="replay",
        status="completed",
    )
    db.add(run)
    db.flush()
    t = load("route-class", True)["turns"][0]
    fit = calibration()["fit"]
    args = dict(
        spans=[Span(**s) for s in t["spans"]],
        cached_tokens=t["cached_tokens"],
        output_tokens=t["output_tokens"],
        alpha=fit["alpha"],
        beta=fit["beta"],
    )
    bill, linear = price_call(**args), price_call(**args, method="linear")
    spans = [dict(s) for s in t["spans"]]
    share = bill.royalty_micros["starlette"]
    entry = next(s for s in spans if s["kind"] == "publisher")
    entry.update(
        slug=source.slug,
        owner=source.publisher_id,
        hits=bill.hits[source.slug],
        royalty_micros=share,
        linear_micros=linear.royalty_micros["starlette"],
    )
    call = Call(
        workspace_id=buyer.id,
        run_id=run.id,
        turn=t["turn"],
        prompt_tokens=t["prompt_tokens"],
        cached_tokens=t["cached_tokens"],
        output_tokens=t["output_tokens"],
        input_hash=t["input_hash"],
        output_text=t["text"],
        ttft_ms=t["ttft_ms"],
        total_ms=t["total_ms"],
        hits_json=bill.hits,
        spans_json=spans,
        royalty_json={source.publisher_id: share},
        public_charge_micros=bill.public_charge_micros,
        charge_micros=bill.charge_micros,
        platform_micros=bill.platform_micros,
        mode="replay",
    )
    db.add(call)
    db.flush()
    return call, mandate, entry


def test_recorded_external_usage_visible_as_aggregates_only(tmp_path):
    from backend.app.agents.copilot_tools import call_details, earnings

    engine = init_db(f"sqlite:///{tmp_path}/usage.db")
    with session_scope(engine) as db:
        buyer, seller = workspace(db, "user"), workspace(db, "user")
        source = layer(db, seller, "starlette")
        call, _, entry = archived_call(db, buyer, source)
        share = entry["royalty_micros"]
        marketplace.record_usage(db, call, [source.id])
        assert marketplace.totals(db, source.id) == (
            entry["hits"],
            share,
            entry["linear_micros"],
        )
        assert str(f"{entry['hits']:,}") in " ".join(earnings(db, seller.id))
        assert list(marketplace.settlement_contexts(db, buyer.id)) == [source]
        assert list(marketplace.settlement_contexts(db, seller.id)) == [source]
        assert marketplace.totals(db, source.id, buyer=buyer.id)[1] == share
        assert marketplace.totals(db, source.id, buyer=seller.id)[1] == 0
        assert call.id not in " ".join(earnings(db, seller.id))
        assert call_details(db, seller.id, call.id) == [
            "No matching metered call was found in this workspace."
        ]


def test_only_an_explicit_final_rounding_subsidy_releases_uncaptured_royalties(
    tmp_path,
):
    engine = init_db(f"sqlite:///{tmp_path}/funding.db")
    with session_scope(engine) as db:
        buyer, seller = workspace(db, "user"), workspace(db, "user")
        ctx = layer(db, seller, "starlette")
        call, mandate, span = archived_call(db, buyer, ctx)
        # Exercise local accrual/rounding on an actual sub-cent reading. No external ID
        # or payment is invented; the merchant bears this explicit rounding expense.
        amount = call.charge_micros
        share = span["royalty_micros"]
        assert 0 < share < amount < 5000
        ledger.post(
            db,
            buyer.id,
            "call",
            call.id,
            {
                ledger.receivable(mandate.consumer_id): amount,
                ledger.REVENUE: share - amount,
                ledger.payable(ctx.publisher_id): -share,
            },
        )
        mandate.accrued_uncaptured_micros = amount
        assert ledger.available_payable(db, seller.id, ctx.publisher_id) == 0
        ledger.close_dust(db, mandate)
        assert ledger.balance(db, buyer.id, ledger.ROUNDING) == amount
        assert ledger.available_payable(db, seller.id, ctx.publisher_id) == share
        for ws in (buyer, seller):
            assert (
                ledger.assert_invariants(
                    db, workspace_id=ws.id, expected_clearing_micros=0
                )
                == 0
            )

        from backend.app.models import PayoutBatch, PayoutItem

        batch = PayoutBatch(
            workspace_id=seller.id, sender_batch_id=uuid4().hex, status="CREATED"
        )
        db.add(batch)
        db.flush()
        pending = PayoutItem(
            workspace_id=seller.id,
            batch_id=batch.id,
            publisher_id=ctx.publisher_id,
            amount_cents=1,
            status="PENDING",
        )
        db.add(pending)
        db.flush()
        assert ledger.available_payable(db, seller.id, ctx.publisher_id) == 0
        assert (
            ledger.available_payable(
                db, seller.id, ctx.publisher_id, excluding_reservation=pending.id
            )
            == share
        )


def test_terminal_check_survives_workspace_state_reload(tmp_path):
    from backend.app.api.finance import saved_application_check
    from backend.app.models import AgentRun
    from backend.app.operations import RunEvent
    from backend.app.replay.recordings import application_evidence

    receipt = application_evidence(load("route-class", True))
    engine = init_db(f"sqlite:///{tmp_path}/terminal.db")
    with session_scope(engine) as db:
        ws = workspace(db, "judge")
        call, _, _ = archived_call(db, ws, layer(db, ws, "starlette"))
        run = db.get(AgentRun, call.run_id)
        assert (
            saved_application_check(db, run)["application_check"]["status"]
            == "unverified"
        )
        db.add(
            RunEvent(
                run_id=run.id,
                event_type="done",
                payload={"run_id": run.id, "status": "completed", **receipt},
            )
        )
    with session_scope(engine, write=False) as db:
        assert saved_application_check(db, db.get(AgentRun, run.id)) == receipt
