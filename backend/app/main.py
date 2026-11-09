from fastapi import FastAPI

app = FastAPI(title="Cachet", version="0.1.0")


@app.get("/api/health")
def health() -> dict[str, str]:
    """Report API liveness independently of inference and payment services."""
    return {"service": "cachet-api", "version": app.version}
