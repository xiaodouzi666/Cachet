from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from time import monotonic

app = FastAPI()

rate_limit = {}

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_host = request.client.host
    timestamps = rate_limit.get(client_host, [])
    now = monotonic()
    timestamps.append(now)
    while timestamps and timestamps[0] < now - 60:
        timestamps.pop(0)
    if len(timestamps) >= 3:
        return JSONResponse(status_code=429, content={"detail": "Too Many Requests"})
    rate_limit[client_host] = timestamps
    response = await call_next(request)
    return response

@app.get("/ping")
async def ping():
    return {"pong": True}
