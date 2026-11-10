import threading
import time
import httpx
from backend.app.config import value
from backend.app.gateway import registration
from backend.app.gateway.hosted import checked_profile, fingerprint
from backend.app.gateway.sglang_client import Engine, settings
from backend.app.replay.recordings import available

_lock = threading.Lock()
_checked = 0.0
_state = None


def health():
    global _state, _checked
    with _lock:
        if _state is not None and time.monotonic() - _checked < 5:
            return dict(_state)
        try:
            if registration.enabled():
                state = registration.active()
                if not state:
                    raise ValueError
                return {"mode": "live", "gpu": state["gpu"], "model": state["model"],
                        "sglang": "online", "sglang_version": state["sglang_version"]}
            url, key = settings()
            if not key:
                raise ValueError
            response = httpx.get(
                url.rstrip("/") + "/model_info",
                headers={"Authorization": f"Bearer {key}"},
                timeout=2,
            )
            response.raise_for_status()
            model = response.json()["model_path"]
            state = {
                "mode": "live",
                "model": model.rsplit("/", 1)[-1],
                "sglang": "online",
            }
        except (ValueError, KeyError, httpx.HTTPError):
            state = {
                "mode": "replay" if available() else "unavailable",
                "sglang": "offline",
            }
            if value("HOSTED_FALLBACK_BASE_URL") and value("HOSTED_FALLBACK_API_KEY") and value("HOSTED_FALLBACK_CALIBRATION"):
                try:
                    with Engine("hosted") as engine:
                        p = checked_profile(engine)
                    state = {"mode":"hosted", "model":p["model"], "gpu":p["gpu"], "sglang":"online",
                             "calibration_id":fingerprint(p)}
                except (ValueError, KeyError, OSError, TypeError, httpx.HTTPError):
                    pass
        _state = state
        _checked = time.monotonic()
        return dict(state)
