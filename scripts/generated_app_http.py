"""Run inside the isolated container. Exercise the generated ASGI app via loopback HTTP."""

from contextlib import closing
import http.client
import json
import socket
import sys
import threading
import time

import uvicorn
from fastapi import FastAPI
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import PlainTextResponse

import generated_app
from generated_app import app


def get(port, address, path):
    with closing(
        http.client.HTTPConnection(
            "127.0.0.1", port, timeout=4, source_address=(address, 0)
        )
    ) as connection:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read(4096), response.getheader("X-Route-Class")


def check_rejected_request_window(port):
    """Exercise sliding-window recovery without changing Uvicorn's real clock."""
    clock = [0.0]

    class TimeProxy:
        def __getattr__(self, name):
            return (lambda: clock[0]) if name == "monotonic" else getattr(time, name)

    bindings = {
        name: value for name, value in vars(generated_app).items()
        if value is time.monotonic or value is time
    }
    assert bindings, "Expected the application's imported monotonic clock"
    try:
        for name, value in bindings.items():
            replacement = TimeProxy() if value is time else (lambda: clock[0])
            setattr(generated_app, name, replacement)
        observed = []
        for instant in (0, 1, 30, 40, 62):
            clock[0] = instant
            # Separate from the clients in the real-time 60-second check.
            code, body, _ = get(port, "127.0.0.3", "/ping")
            observed.append(code)
            if code == 200:
                assert json.loads(body) == {"pong": True}
        expected = [200, 200, 429, 429, 200]
        assert observed == expected, (
            "Rejected requests must not consume quota: at monotonic seconds "
            f"[0, 1, 30, 40, 62], expected {expected}, observed {observed}. "
            "Prune expired accepted timestamps, reject if two remain, and append only after admission."
        )
    finally:
        for name, value in bindings.items():
            setattr(generated_app, name, value)


def check(task, full):
    if task == "rate-limit":
        assert isinstance(app, FastAPI) and app.user_middleware, (
            "Expected a FastAPI app with registered middleware"
        )
    else:
        assert isinstance(app, Starlette)
        routes = [r for r in app.routes if getattr(r, "path", None) == "/ping"]
        custom = next(
            (r for r in routes if isinstance(r, Route) and type(r) is not Route), None
        )
        assert custom is not None, "Expected a custom Starlette Route"

        async def neutral(request):
            return PlainTextResponse("probe")

        app.router.routes.append(type(custom)("/contract-probe", endpoint=neutral))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", lifespan="off", proxy_headers=False)
        )
        worker = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        worker.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.015)
        assert server.started, "Generated app did not start"
        try:
            if task == "rate-limit":
                observed = [
                    get(port, address, "/ping")
                    for address in ["127.0.0.1", "127.0.0.1", "127.0.0.1", "127.0.0.2"]
                ]
                codes = [r[0] for r in observed]
                assert codes == [200, 200, 429, 200], (
                    f"Unexpected per-client HTTP status sequence {codes}"
                )
                assert all(
                    json.loads(body) == {"pong": True}
                    for code, body, _ in observed
                    if code == 200
                )
                json.loads(observed[2][1])
                check_rejected_request_window(port)
                if not full:
                    return {"status_codes": codes, "distinct_clients": 2}
                time.sleep(60.1)
                reset_status, reset_body, _ = get(port, "127.0.0.1", "/ping")
                assert reset_status == 200 and json.loads(reset_body) == {
                    "pong": True
                }, "The original client did not recover after 60 seconds"
                return {
                    "status_codes": codes + [reset_status],
                    "success_body": {"pong": True},
                    "distinct_clients": 2,
                    "window_seconds": 60,
                }
            code, body, header = get(port, "127.0.0.1", "/ping")
            missing, _, _ = get(port, "127.0.0.1", "/missing")
            assert (
                code == 200
                and json.loads(body) == {"pong": True}
                and header == "custom"
            ), f"Unexpected route response {code}, header={header!r}"
            assert missing == 404, "Expected HTTP 404 for an unregistered route"
            probe, probe_body, probe_header = get(port, "127.0.0.1", "/contract-probe")
            assert (probe, probe_body, probe_header) == (200, b"probe", "custom"), (
                "The Route subclass must add the header to a neutral endpoint"
            )
            return {
                "status_codes": [code, missing, probe],
                "success_body": {"pong": True},
                "x_route_class": header,
                "subclass_probe": True,
            }
        finally:
            server.should_exit = True
            worker.join(timeout=2)


if __name__ == "__main__":
    print(json.dumps(check(sys.argv[1], sys.argv[2] == "full"), sort_keys=True))
