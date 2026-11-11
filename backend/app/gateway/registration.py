"""Discover the live GPU from authenticated, short-lived heartbeats."""
import hashlib
import hmac
import logging
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

from backend.app.config import registered_inference as enabled, value
from backend.app.db import session_scope
from backend.app.operations import Control
from backend.app.gateway.tunnel_transport import TunnelTransport

REGISTRY_KEY = "inference:registered-endpoint"
HEARTBEAT_TTL = 90
PROBE_INTERVAL = 30
PROBE_TTL = 45
QUICK_HOST = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com")
_probe_lock = threading.Lock()
logger = logging.getLogger(__name__)


def origin_for(url, fixed=""):
    # Fixed origins are provisioned by the operator. Quick Tunnel hostnames use Cloudflare TLS.
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.netloc != host or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or not host):
        raise ValueError("Expected an HTTPS tunnel origin without a port, path or credentials.")
    origin = f"https://{host}"
    if not QUICK_HOST.fullmatch(host) and origin != fixed.rstrip("/"):
        raise ValueError("The tunnel must use the configured fixed origin or a Cloudflare Quick Tunnel.")
    return origin


def tunnel_token(key, origin):
    # A retired or reassigned public hostname must never receive the engine's own long-lived key.
    return hmac.new(key.encode(), ("cachet-gpu-tunnel-v1:" + origin).encode(), hashlib.sha256).hexdigest()


def heartbeat(metadata):
    origin = origin_for(metadata["url"], value("TUNNEL_FIXED_URL"))
    with session_scope() as db:
        previous = db.get(Control, REGISTRY_KEY)
        record = dict(previous.data) if previous and previous.data["url"] == origin else {}
        record.update(metadata, url=origin, seen_at=time.time())
        if previous:
            previous.data = record
        else:
            db.add(Control(key=REGISTRY_KEY, data=record))
    probe()
    return {"registered": True, "live": bool(active()), "heartbeat_ttl_seconds": HEARTBEAT_TTL}


def record():
    with session_scope(write=False) as db:
        row = db.get(Control, REGISTRY_KEY)
        return dict(row.data) if row else {}


def fresh(stamp, ttl):
    return 0 <= time.time() - stamp < ttl


def probe_origin(origin, key):
    with httpx.Client(transport=TunnelTransport(origin), timeout=4, follow_redirects=False) as client:
        response = client.get(origin + "/health", headers={"Authorization": "Bearer " + tunnel_token(key, origin)})
        return response.status_code == 200


def probe():
    with _probe_lock:
        state = record()
        if not state or not fresh(state["seen_at"], HEARTBEAT_TTL) or fresh(state.get("checked_at", 0), PROBE_INTERVAL if state.get("ok") else 5):
            return
        origin = origin_for(state["url"], value("TUNNEL_FIXED_URL"))
        key = value("SGLANG_API_KEY")
        ok = False
        if key:
            try:
                ok = probe_origin(origin, key)
            except (ValueError, httpx.HTTPError):
                pass
        with session_scope() as db:
            row = db.get(Control, REGISTRY_KEY)
            if row and row.data["url"] == origin:
                row.data = {**row.data, "ok": ok, "checked_at": time.time()}


def active():
    state = record()
    if (not state.get("ok") or not fresh(state["seen_at"], HEARTBEAT_TTL)
            or not fresh(state["checked_at"], PROBE_TTL)):
        return None
    return state


def connection():
    state = active()
    key = value("SGLANG_API_KEY")
    if not state or not key:
        raise ValueError("The GPU has no recent, verified endpoint registration.")
    origin = origin_for(state["url"], value("TUNNEL_FIXED_URL"))
    return origin, tunnel_token(key, origin)


def monitor():
    stop = threading.Event()
    if enabled():
        def run():
            while not stop.is_set():
                try:
                    probe()
                except Exception:
                    logger.exception("The registered inference health check could not complete")
                stop.wait(5)
        threading.Thread(target=run, name="gpu-endpoint-health", daemon=True).start()
    return stop
