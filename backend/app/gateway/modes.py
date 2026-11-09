import threading
import time
import httpx
from backend.app.gateway.sglang_client import settings
from backend.app.replay.recordings import available

_lock = threading.Lock()
_checked = 0.0
_state = None


def health():
    global _state, _checked
    with _lock:
        if _state is not None and time.monotonic() - _checked < 5:
            return dict(_state)
        url, key = settings()
        try:
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
        _state = state
        _checked = time.monotonic()
        return dict(state)
