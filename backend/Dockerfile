FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv/cachet
COPY requirements.txt backend/requirements.txt
RUN python -m pip install --no-cache-dir -r backend/requirements.txt
COPY install_toolkit.py backend/install_toolkit.py
RUN PIP_NO_CACHE_DIR=1 python backend/install_toolkit.py
COPY --chown=1000:1000 app backend/app
COPY --chown=1000:1000 migrations backend/migrations
COPY --chown=1000:1000 __init__.py backend/__init__.py
COPY --chown=1000:1000 runtime/contexts contexts
COPY --chown=1000:1000 runtime/calibration.json bench/results/calibration.json
RUN useradd --uid 1000 --create-home cachet && mkdir -p /var/data && chown cachet:cachet /var/data
USER cachet
ENV DATABASE_URL=sqlite:////var/data/cachet.db
EXPOSE 10000
CMD ["sh", "-c", "exec python -m uvicorn backend.app.main:app --host 0.0.0.0 --port \"${PORT:-10000}\" --workers 1"]
