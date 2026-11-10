import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.db import session_scope
from backend.app.gateway import modes, registration
from backend.app.main import app
from backend.app.operations import Control
from scripts import gpu_proxy

ORIGIN = "https://example-gpu.trycloudflare.com"
META = {"url": ORIGIN, "sglang_version": "0.5.23", "model": "test-model", "gpu": "test-gpu"}


def test_durable_discovery_rotation_loss_and_expiry(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/registry.sqlite")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    monkeypatch.setenv("SGLANG_API_KEY", "test-gpu-secret")
    monkeypatch.setenv("INFERENCE_ENDPOINT_MODE", "registered")
    calls = []
    result = [200]

    def health_get(origin, key):
        calls.append((origin+"/health", {"Authorization":"Bearer "+registration.tunnel_token(key,origin)}))
        return result[0] == 200
    monkeypatch.setattr(registration, "probe_origin", health_get)
    client = TestClient(app)
    route = "/api/admin/inference-endpoint"
    assert client.post(route, json=META).status_code == 403 and not calls
    headers = {"X-Admin-Token":"test-admin"}
    for candidate in ["http://127.0.0.1", "https://example.com", ORIGIN+"/path", ORIGIN+":443", ORIGIN+"?token=ab"]:
        assert client.post(route, headers=headers, json={**META, "url":candidate}).status_code == 422
    assert not calls
    assert client.post(route, headers=headers, json=META).json()["live"]
    assert registration.connection() == (ORIGIN, registration.tunnel_token("test-gpu-secret", ORIGIN))
    assert calls[-1][0] == ORIGIN+"/health"
    assert calls[-1][1]["Authorization"] == "Bearer "+registration.connection()[1]
    second = "https://rotated.trycloudflare.com"
    assert client.post(route, headers=headers, json={**META,"url":second}).json()["live"]
    assert registration.connection()[0] == second and calls[-1][1] != calls[0][1]
    assert client.get("/api/health").json()["gpu"] == META["gpu"]
    result[0] = 503
    with session_scope() as db:
        row = db.get(Control, registration.REGISTRY_KEY)
        row.data = {**row.data, "checked_at": 0}
    registration.probe()
    assert not registration.active()
    with pytest.raises(ValueError): registration.connection()
    monkeypatch.setattr(modes,"_state",None)
    assert client.get("/api/health").json()["mode"] == "replay"
    with session_scope() as db:
        row = db.get(Control, registration.REGISTRY_KEY)
        row.data = {**row.data, "ok":True, "seen_at":registration.time.time()-91}
    assert not registration.active()


def test_only_per_origin_credentials_reach_loopback_engine(tmp_path, monkeypatch):
    state = tmp_path / "edge.json"
    state.write_text('{"url":"'+ORIGIN+'"}')
    monkeypatch.setattr(gpu_proxy,"STATE",state)
    monkeypatch.setenv("SGLANG_API_KEY","test-native")
    seen = []
    def handle(request):
        seen.append((request.url.path, request.headers["authorization"]))
        return httpx.Response(200, text="ready")
    import asyncio
    client = TestClient(gpu_proxy.app)
    upstream = httpx.AsyncClient(base_url="http://127.0.0.1:30000", transport=httpx.MockTransport(handle))
    gpu_proxy.app.state.engine = upstream
    try:
        assert client.get("/health").status_code == 401
        assert client.get("/health", headers={"Authorization":"Bearer test-native"}).status_code == 401
        scoped = {"Authorization":"Bearer "+registration.tunnel_token("test-native",ORIGIN)}
        assert client.get("/health",headers=scoped).text == "ready"
        assert client.get("/server_info",headers=scoped).status_code == 404
        state.write_text('{"url":"https://rotated.trycloudflare.com"}')
        assert client.get("/health",headers=scoped).status_code == 401
        assert seen == [("/health","Bearer test-native")]
    finally:
        asyncio.run(upstream.aclose())


def test_quick_dns_recovery_keeps_tls_identity(monkeypatch):
    import socket
    from backend.app.gateway import tunnel_transport

    host = httpx.URL(ORIGIN).host
    def fresh_lookup(*args, **kwargs):
        raise socket.gaierror("new tunnel pending in local DNS")
    monkeypatch.setattr(socket, "getaddrinfo", fresh_lookup)
    monkeypatch.setattr(tunnel_transport, "resolve_quick_tunnel", lambda hostname: "104.16.230.132" if hostname == host else None)
    recorded = []
    def perform(transport, req):
        recorded.append(req)
        return httpx.Response(200, text="ready")
    monkeypatch.setattr(httpx.HTTPTransport,"handle_request",perform)
    with httpx.Client(transport=tunnel_transport.TunnelTransport(ORIGIN)) as http:
        assert http.get(ORIGIN + "/health").is_success
    assert str(recorded[0].url) == "https://104.16.230.132/health"
    assert recorded[0].headers["host"] == host and recorded[0].extensions["sni_hostname"] == host
