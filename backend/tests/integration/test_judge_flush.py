"""Exercise the judge's cold and delayed warm turns on the real primary GPU."""

import json
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.agents.inference import live_turns
from backend.app.agents.quote_reason import generate_reason
from backend.app.agents.tasks import TASKS
from backend.app.api.agent import next_fault
from backend.app.api.catalog import observed_live_call, pending_flush_id
from backend.app.api.finance import router
from backend.app.config import band_for, calibration
from backend.app.gateway.billing import Span, price_call, sla_breached
from backend.app.gateway.prompt_builder import Prefix
from backend.app.gateway.quote import quote


@pytest.mark.gpu
def test_judge_flush_then_fault(engine, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'judge.sqlite'}")
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("ADMIN_TOKEN", token)
    monkeypatch.setenv("PUBLIC_WEB_URL", "http://localhost")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, headers={"X-Admin-Token": token}) as client:
        def control(route, body):
            return client.post("/api/judge/" + route, json=body).raise_for_status().json()

        other = control("workspace", {"kind": "user"})["workspace_id"]
        judge = control("workspace", {"kind": "judge"})["workspace_id"]
        prefix = Prefix(engine, "fastapi")
        assert prefix.warm(engine) >= .95

        before = pending_flush_id(judge)
        receipt = control("flush-cache", {})
        marker = receipt["flush_id"]
        assert pending_flush_id(other) is None
        observed_live_call(judge, before)
        observed_live_call(other, marker)
        assert pending_flush_id(judge) == marker

        control("fault", {"ttft_delay_ms": 1000, "calls": 2})
        terms = quote(sum(len(ids) for _, ids in prefix.layers), 10, .25)
        assert generate_reason(engine, terms)["source"] == "live"
        assert pending_flush_id(judge) == marker

        owners = {slug: slug for slug, _ in prefix.layers}
        stream = live_turns(
            engine, prefix, owners, secrets.token_hex(4), TASKS["rate-limit"][1], True,
            max_output=16,
            fault_source=lambda: next_fault(judge),
            pending_flush=lambda: pending_flush_id(judge),
        )
        profile = calibration()

        def metered_turn(turn):
            estimate = next(stream)
            assert estimate["kind"] == "estimate" and estimate["turn"] == turn
            measured = next(stream)
            assert measured["kind"] == "result" and measured["turn"] == turn
            priced = price_call(
                spans=[Span(**span) for span in measured["spans"]],
                cached_tokens=measured["cached_tokens"], output_tokens=measured["output_tokens"],
                alpha=profile["fit"]["alpha"], beta=profile["fit"]["beta"],
            )
            return measured, priced

        cold, cold_price = metered_turn(1)
        assert cold["cache_flush_id"] == marker
        assert cold["cached_tokens"] == cold_price.public_charge_micros == 0
        assert cold["fault_injected_ms"] >= 1000
        assert not any(cold_price.royalty_micros.values())
        observed_live_call(judge, marker)
        assert pending_flush_id(judge) is None

        warm, price = metered_turn(2)
        assert "cache_flush_id" not in warm and warm["fault_injected_ms"] >= 1000
        assert all(price.hits[slug] > 0 for slug in owners)
        assert price.public_charge_micros > 0 and all(price.royalty_micros.values())
        public_hits = sum(price.hits[slug] for slug in owners)
        baseline = band_for(warm["prompt_tokens"], profile)["warm_p50_ms"]
        assert sla_breached(public_hits, warm["ttft_ms"], baseline)
        stream.close()
        print(json.dumps({
            "cold_hits": cold["cached_tokens"], "cold_public_micros": cold_price.public_charge_micros,
            "warm_hits": public_hits, "warm_ttft_ms": warm["ttft_ms"],
            "injected_ms": warm["fault_injected_ms"], "warm_sla_ms": 2 * baseline,
        }))
