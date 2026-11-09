FROM python:3.11-slim
RUN python -m pip install --no-cache-dir fastapi==0.162.2 starlette==1.10.0 uvicorn==0.54.0
WORKDIR /case
USER 65534:65534
