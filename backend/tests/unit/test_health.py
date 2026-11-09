from fastapi.testclient import TestClient

from backend.app.main import app


def test_health_reports_api_identity_and_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'health.sqlite3'}")
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["service"] == "cachet-api"
    assert response.json()["version"] == app.version
