from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import finance


def test_gateway_is_scoped_to_workspace_routes(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "operator-evidence")
    monkeypatch.setenv("BFF_TOKEN", "workspace-evidence")
    app = FastAPI()
    app.include_router(finance.router)
    with TestClient(app) as client:
        web = {"X-Gateway-Token": "workspace-evidence"}
        assert client.get("/api/judge/state", headers=web).status_code == 401
        assert client.post("/api/operator/seed-runs/any", headers=web).status_code == 403
        assert client.get("/api/judge/state").status_code == 403
