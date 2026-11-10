"""Expose the inference operations Cachet uses and keep the engine admin surface on loopback."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets

import httpx
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from backend.app.config import ROOT, value
from backend.app.gateway.registration import origin_for, tunnel_token

STATE = ROOT / "logs/gpu-agent/edge.json"
READ_ROUTES = {"health", "model_info", "v1/models"}
WRITE_ROUTES = {"tokenize", "detokenize", "generate", "flush_cache", "v1/chat/completions"}
LIMIT = 2 * 1024 * 1024


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(base_url="http://127.0.0.1:30000", timeout=180, trust_env=False,
                                 follow_redirects=False) as engine:
        app.state.engine = engine
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def checked_headers(request):
    try:
        origin = origin_for(json.loads(STATE.read_text())["url"], value("TUNNEL_FIXED_URL"))
    except (OSError, KeyError, ValueError):
        raise HTTPException(503, "Tunnel is not registered")
    key = value("SGLANG_API_KEY")
    if not key or not secrets.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + tunnel_token(key, origin)).encode()):
        raise HTTPException(401, "Inference credentials required")
    return {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept-Encoding": "identity"}


async def limited_body(request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > LIMIT:
            raise HTTPException(413, "Inference input exceeds the size limit")
        data.extend(chunk)
    return bytes(data)


@app.api_route("/{route:path}", methods=["GET", "POST"])
async def relay(route: str, request: Request):
    if route not in (READ_ROUTES if request.method == "GET" else WRITE_ROUTES):
        raise HTTPException(404)
    headers = checked_headers(request)
    query = {}
    if request.query_params:
        if route != "flush_cache" or list(request.query_params.keys()) != ["timeout"]:
            raise HTTPException(400, "Unsupported inference parameters")
        timeout = request.query_params["timeout"]
        if not timeout.isdigit() or not 1 <= int(timeout) <= 30:
            raise HTTPException(400, "Invalid flush timeout")
        query["timeout"] = timeout
    body = await limited_body(request)
    client = request.app.state.engine
    upstream = client.build_request(request.method, "/" + route, params=query, content=body, headers=headers)
    try:
        response = await client.send(upstream, stream=True)
    except httpx.RequestError:
        raise HTTPException(503, "The local inference server is unavailable")
    return StreamingResponse(response.aiter_bytes(), status_code=response.status_code,
        headers={"Content-Type": response.headers.get("content-type", "application/octet-stream"),
                 "Cache-Control": "no-store", "X-Accel-Buffering": "no"}, background=BackgroundTask(response.aclose))
