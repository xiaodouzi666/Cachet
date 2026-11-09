"""Exercise the real ASGI receiver's input boundaries without a sandbox account."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.webhooks import MAX_BODY, router


def test_rejects_malformed_and_oversize_requests_before_ingestion():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        for payload in (b"", b"[]", b"{}"):
            assert client.post("/api/webhooks/paypal", content=payload).status_code == 400
        assert client.post("/api/webhooks/paypal", content=b" " * (MAX_BODY + 1)).status_code == 413
