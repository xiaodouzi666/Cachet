from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.webhooks import router
from backend.app.db import session_scope
from backend.app.models import WebhookEvent, utcnow
from backend.app.paypal import webhooks


def test_operator_filters_signed_resource_type_and_payout_aliases(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/receiver.db")
    monkeypatch.setenv("ADMIN_TOKEN", "operator-test")
    monkeypatch.setenv("BFF_TOKEN", "gateway-test")
    created, voided = "PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.AUTHORIZATION.VOIDED"
    batch, item = "PAYMENT.PAYOUTSBATCH.SUCCESS", "PAYMENT.PAYOUTS-ITEM.SUCCEEDED"
    records = [
        ("one", created, "auth", {"id": "auth"}, True),
        ("two", voided, "auth", {"id": "auth"}, True),
        ("three", created, "auth", {"id": "auth"}, False),
        ("four", batch, "wrapper", {"batch_header": {"payout_batch_id": "batch"}}, True),
        ("five", item, "envelope", {"payout_item_id": "item", "payout_batch_id": "batch"}, True),
    ]
    timestamp = utcnow()
    with session_scope() as session:
        for seq, (event_id, name, rid, payload, verified) in enumerate(records):
            session.add(WebhookEvent(paypal_event_id=event_id, event_type=name, resource_id=rid,
                raw_json={"resource": payload, "private": "never-serialize"}, verified=verified,
                received_at=timestamp + timedelta(seconds=seq)))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        def receipt(rid, kind=None, **headers):
            return client.get("/api/operator/webhook-receipts/" + rid, headers=headers,
                              params={"event_type": kind} if kind else {})
        admin = {"X-Admin-Token": "operator-test"}
        assert receipt("auth").status_code == 403
        assert receipt("auth", created, **{"X-Gateway-Token": "gateway-test"}).status_code == 403
        assert receipt("auth", **admin).json()["event_id"] == "two"
        for rid, kind, event in (("auth", created, "one"), ("auth", voided, "two"),
                                 ("batch", batch, "four"), ("item", item, "five")):
            body = receipt(rid, kind, **admin).json()
            assert (body["event_id"], body["resource_id"], body["type"], body["verified"]) == (event, rid, kind, True)
            assert set(body) == {"event_id", "resource_id", "type", "verified", "received_at", "processed_at"}
        assert receipt("batch", item, **admin).status_code == 404
        assert receipt("not-delivered", created, **admin).status_code == 404
        assert receipt("auth", "unknown", **admin).status_code == 422


def test_remote_verification_uses_only_bound_origin_and_event(monkeypatch):
    kind, other = "CHECKOUT.ORDER.APPROVED", "PAYMENT.AUTHORIZATION.CREATED"
    expected = {kind: {"known", "wrong-type", "wrong-resource", "unsigned", "absent"}}

    def serve(request):
        assert request.url.host == "receiver.example.test"
        assert request.headers["X-Admin-Token"] == "scoped-operator"
        assert request.url.params == httpx.QueryParams(event_type=kind)
        rid = request.url.path.rsplit("/", 1)[1]
        if rid == "absent":
            return httpx.Response(404)
        return httpx.Response(200, json={"event_id": "test-" + rid, "resource_id": "unknown" if rid == "wrong-resource" else rid,
            "type": other if rid == "wrong-type" else kind, "verified": rid != "unsigned", "received_at": "2026-01-01T00:00:00Z"})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(serve), **kw))
    config = {"PRODUCTION_API_URL": "https://receiver.example.test", "PRODUCTION_ADMIN_TOKEN": "scoped-operator",
              "PUBLIC_API_URL": "https://different.example.test", "ADMIN_TOKEN": "dev-test"}
    evidence = webhooks._remote_receipts(expected, config)
    assert evidence["missing"] == {kind: ["absent", "unsigned", "wrong-resource", "wrong-type"]}
    assert [row["event_id"] for row in evidence["verified_deliveries"]] == ["test-known"]
    for invalid in ("http://receiver.example.test", "https://receiver.example.test/api", ""):
        with pytest.raises(ValueError):
            webhooks._remote_receipts(expected, {**config, "PRODUCTION_API_URL": invalid})


def test_receipt_source_tracks_the_webhook_id(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/empty.db")
    monkeypatch.setattr(webhooks, "expected_deliveries", lambda *_: {"CHECKOUT.ORDER.APPROVED": {"unseen"}})
    cfg = {"PAYPAL_WEBHOOK_ID": "prod", "PRODUCTION_PAYPAL_WEBHOOK_ID": "prod"}
    monkeypatch.setattr(webhooks, "settings", lambda: cfg)
    monkeypatch.setattr(webhooks, "_remote_receipts", lambda *_: "remote")
    assert webhooks.delivery_evidence({}, {}) == "remote"
    cfg["PAYPAL_WEBHOOK_ID"] = "development"
    assert webhooks.delivery_evidence({}, {})["missing"] == {"CHECKOUT.ORDER.APPROVED": ["unseen"]}


def test_wait_for_receipts_retries_transient_reads_but_reports_denied_auth(monkeypatch):
    original = {"verified_deliveries": [{"event_id": "test-event"}], "missing": {}}
    replies = iter([httpx.ConnectTimeout("temporary receiver timeout"), original])

    def read(*_):
        result = next(replies)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(webhooks, "delivery_evidence", read)
    monkeypatch.setattr(webhooks.time, "sleep", lambda *_: None)
    assert webhooks.wait_for_deliveries({}, {}, 1) == original
    response = httpx.Response(403, request=httpx.Request("GET", "https://receiver.example.test"))
    monkeypatch.setattr(webhooks, "delivery_evidence", lambda *_: response.raise_for_status())
    with pytest.raises(httpx.HTTPStatusError):
        webhooks.wait_for_deliveries({}, {}, 180)
