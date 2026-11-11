from uuid import uuid4

from backend.app.agents.anomaly import volume_signal
from backend.app.agents.copilot_tools import (
    earnings,
    ledger_activity,
    mandate_breakdown,
    overview,
)
from backend.app.db import init_db, session_scope
from backend.app.models import Consumer, Mandate, Workspace


def test_structured_intent_preserves_id_and_never_proposes_for_a_negation(monkeypatch):
    from backend.app.agents import copilot

    key = uuid4().hex
    monkeypatch.setattr(copilot, "chat", lambda *_: {"tool": "unsupported", "arguments": {}})
    tools = copilot.LOCAL_DEFINITIONS
    assert copilot.route(f"Refund call {key}.", tools) == ("propose_refund", {"call_id": key})
    assert copilot.route(f"Do not refund call {key}.", tools) == ("call_details", {"call_id": key})
    assert copilot.route(f"Where did the money from mandate {key} go?", tools) == (
        "mandate_breakdown", {"mandate_id": key})
    assert copilot.route("How much did Starlette earn this week?", tools) == (
        "publisher_earnings", {"days": 7})


def test_new_workspace_evidence_and_scoped_mandate(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path}/evidence.db")
    with session_scope(engine) as db:
        first, second = [Workspace(cookie_id=uuid4().hex) for _ in range(2)]
        db.add_all([first, second])
        db.flush()
        owner = Consumer(
            workspace_id=first.id, name="Unit test", salt_hex=uuid4().hex[:8]
        )
        db.add(owner)
        db.flush()
        # The local draft has no remote order, authorization, captures or payment IDs.
        mandate = Mandate(
            workspace_id=first.id, consumer_id=owner.id, cap_micros=5_000_000
        )
        db.add(mandate)
        db.flush()
    with session_scope(engine, write=False) as db:
        assert "There are no publishers" in earnings(db, second.id)[0]
        assert "no posted entries" in ledger_activity(db, second.id)[0]
        assert "0 mandate records" in overview(db, second.id)[0]
        assert mandate.id in " ".join(mandate_breakdown(db, first.id, mandate.id))
        assert "No matching mandate" in mandate_breakdown(db, second.id, mandate.id)[0]


def test_spike_requires_historical_variance():
    assert volume_signal(10, [0] * 7) is None
    assert volume_signal(10, [2, 1]) is None
    assert volume_signal(100, [5, 5, 6, 5, 4, 5, 5]) > 3


def test_same_payer_suppresses_only_its_layer_on_a_real_gpu_trace():
    """Counterfactual ownership of recorded traffic, without posting a test payment."""
    from backend.app.agents.anomaly import receiver_digest, same_payer_layers
    from backend.app.config import calibration
    from backend.app.gateway.billing import Span, price_call
    from backend.app.models import Publisher
    from backend.app.replay.recordings import load

    trace = load("rate-limit", True)["turns"][0]
    spans = [Span(**span) for span in trace["spans"]]
    fit = calibration()["fit"]
    evidence = dict(
        spans=spans,
        cached_tokens=trace["cached_tokens"],
        output_tokens=trace["output_tokens"],
        alpha=fit["alpha"],
        beta=fit["beta"],
    )
    # These are ephemeral principals for an identity equality test. No provider IDs
    # or transactions are created, inserted in the database, or returned as results.
    principal, other = uuid4().hex, uuid4().hex
    mailbox = (
        uuid4().hex
    )  # Compare canonicalized strings without constructing a recipient address.
    pubs = {
        "starlette": Publisher(paypal_receiver_email=mailbox.upper()),
        "fastapi": Publisher(linked_payer_id=other),
    }
    owners = same_payer_layers(
        {"payer_id": principal, "email_digest": receiver_digest(mailbox)}, pubs
    )
    normal = price_call(**evidence)
    suppressed = price_call(**evidence, self_publishers=owners)
    assert owners == {"starlette"}
    assert suppressed.charge_micros == normal.charge_micros
    assert (
        normal.royalty_micros["starlette"] > 0
        and suppressed.royalty_micros["starlette"] == 0
    )
    assert suppressed.royalty_micros["fastapi"] == normal.royalty_micros["fastapi"] > 0
    assert (
        suppressed.platform_micros - normal.platform_micros
        == normal.royalty_micros["starlette"]
    )
    assert same_payer_layers({"payer_id": other}, pubs) == {"fastapi"}
    assert same_payer_layers({"payer_id": None}, pubs) == set()
