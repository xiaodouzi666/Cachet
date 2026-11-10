import json
from copy import deepcopy
from statistics import median
from backend.app import config
from backend.app.gateway import route_calibration as route


def test_deployed_latency_uses_current_hardware_and_retains_replay_and_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/route.sqlite")
    monkeypatch.setenv("INFERENCE_ENDPOINT_MODE", "registered")
    identity = {"model": "example-model", "gpu": "example-device", "sglang_version": "test", "service": "test-service"}
    monkeypatch.setattr(route,"identity",lambda:identity)
    archived = config.calibration("replay")
    # Later native calibration can change both Shapley coefficients and SLA bands.
    local = deepcopy(config.calibration("live"))
    local["fit"]["alpha"] *= 1.1
    local["bands"][-1]["warm_p50_ms"] += 10
    new_profile = tmp_path / "bench/results/calibration.json"
    new_profile.parent.mkdir(parents=True)
    new_profile.write_text(json.dumps(local))
    monkeypatch.setattr(config, "ROOT", tmp_path)
    deployment = deepcopy(local["bands"])
    for band in deployment:
        for pair in band["runs"]:
            for side in ("cold", "warm"):
                pair[side]["ttft_ms"] += 250
        for side in ("cold", "warm"):
            band[side+"_p50_ms"] = median(pair[side]["ttft_ms"] for pair in band["runs"])
    route.save({"source":identity, "status":"running", "bands":deployment})
    assert config.calibration("live") == local
    route.save({"source":identity, "status":"completed", "bands":deployment, "measured_at":"2026-01-01T00:00:00Z"})
    assert config.calibration("live")["bands"] == deployment
    assert config.calibration("live")["fit"] == local["fit"]
    assert config.calibration("replay") == archived
    monkeypatch.setattr(route,"identity",lambda:{**identity,"gpu":"another-device"})
    assert config.calibration("live") == local
