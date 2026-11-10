"""Maintain the authenticated GPU tunnel and publish its current origin to Cachet."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.config import value
from backend.app.gateway.registration import origin_for

DIRECTORY = ROOT / "logs/gpu-agent"
EDGE = DIRECTORY / "edge.json"
PAUSE = DIRECTORY / "paused"
SESSION = "cachet-gpu-agent"
STOP = threading.Event()
QUICK_URL = re.compile(rb"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com\b")
PROXY_PORT = 18765


def log(message):
    print(datetime.now(timezone.utc).isoformat(timespec="seconds"), message, flush=True)


def finish(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def launch(stack, args, logfile, env=None):
    output = stack.enter_context(logfile.open("ab"))
    process = subprocess.Popen(args, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, env=env)
    stack.callback(finish, process)
    return process


def metadata(local):
    model = local.get("/model_info").raise_for_status().json()["served_model_name"]
    version = subprocess.check_output([str(ROOT / ".venv-sglang/bin/python"), "-c",
        "from importlib.metadata import version;print(version('sglang'))"], text=True, timeout=10).strip()
    gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader", "-i", "0"], text=True, timeout=10).strip()
    return {"model": model, "gpu": gpu, "sglang_version": version}


def run_tunnel(proxy, api_url, admin_key, local, info):
    fixed = value("TUNNEL_FIXED_URL")
    connector = str(ROOT / ".tools/bin/cloudflared")
    path = DIRECTORY / "cloudflared.log"
    start = path.stat().st_size if path.exists() else 0
    with ExitStack() as stack:
        if fixed:
            origin_for(fixed, fixed)
            env = dict(os.environ, TUNNEL_TOKEN=value("CLOUDFLARE_TUNNEL_TOKEN"))
            if not env["TUNNEL_TOKEN"]:
                raise RuntimeError("The fixed tunnel requires its managed connector token")
            args = [connector,"tunnel","--no-autoupdate","run"]
        else:
            env = None
            args = [connector,"tunnel","--no-autoupdate","--url",f"http://127.0.0.1:{PROXY_PORT}"]
        edge = launch(stack, args, path, env)
        origin = fixed.rstrip("/") if fixed else None
        started = time.monotonic()
        last_sent = float("-inf")
        interval = 30
        failures = 0
        with httpx.Client(timeout=8, follow_redirects=False) as api:
            while not STOP.is_set() and not PAUSE.exists():
                if proxy.poll() is not None:
                    raise RuntimeError("Inference proxy stopped")
                if edge.poll() is not None:
                    return
                if not origin:
                    with path.open("rb") as handle:
                        handle.seek(start)
                        match = QUICK_URL.search(handle.read())
                    if match:
                        origin = origin_for(match.group().decode())
                    elif time.monotonic() - started > 60:
                        return
                if origin and time.monotonic() - last_sent >= interval:
                    tmp = EDGE.with_suffix(".tmp")
                    tmp.write_text(json.dumps({"url":origin}))
                    os.replace(tmp,EDGE)
                    try:
                        local.get("/health").raise_for_status()
                        response = api.post(api_url + "/api/admin/inference-endpoint",
                            headers={"X-Admin-Token":admin_key}, json={"url":origin, **info})
                        response.raise_for_status()
                        receipt = response.json()
                        if receipt.get("registered") is not True:
                            raise RuntimeError("Registration was not acknowledged")
                        if last_sent == float("-inf"):
                            log("Registered inference tunnel")
                        last_sent = time.monotonic()
                        if receipt.get("live"):
                            interval, failures = 30, 0
                        else:
                            interval, failures = 5, failures + 1
                            if failures >= 24:
                                log("Recreating an unreachable public GPU tunnel")
                                return
                    except httpx.HTTPError as exc:
                        log("Heartbeat unavailable: " + type(exc).__name__)
                        if not STOP.wait(3):
                            try:
                                local.get("/health").raise_for_status()
                            except httpx.HTTPError:
                                return
                STOP.wait(1)
    EDGE.unlink(missing_ok=True)


def serve():
    api_url = value("PUBLIC_API_URL").rstrip("/")
    parsed = urlsplit(api_url)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path or
            not (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1","localhost"}))):
        raise SystemExit("Set PUBLIC_API_URL to the trusted Cachet backend origin")
    token = value("ADMIN_TOKEN") if parsed.hostname in {"127.0.0.1", "localhost"} else (value("PRODUCTION_ADMIN_TOKEN") or value("ADMIN_TOKEN"))
    gpu_key = value("SGLANG_API_KEY")
    if not token or not gpu_key:
        raise SystemExit("Configure backend and GPU credentials before starting discovery")
    with (DIRECTORY / "agent.lock").open("a") as lock, ExitStack() as stack:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proxy = launch(stack, [sys.executable, "-m","uvicorn","scripts.gpu_proxy:app",
            "--host","127.0.0.1","--port",str(PROXY_PORT),"--no-access-log"],DIRECTORY/"proxy.log")
        with httpx.Client(base_url="http://127.0.0.1:30000", headers={"Authorization":"Bearer "+gpu_key},
                          timeout=5, trust_env=False) as local:
            while not STOP.is_set():
                if proxy.poll() is not None:
                    raise SystemExit("GPU proxy stopped")
                if PAUSE.exists():
                    EDGE.unlink(missing_ok=True)
                    STOP.wait(1)
                    continue
                try:
                    local.get("/health").raise_for_status()
                except httpx.HTTPError:
                    subprocess.run([str(ROOT/"scripts/start_sglang.sh")],cwd=ROOT,timeout=60,check=False)
                    STOP.wait(8)
                    continue
                try:
                    run_tunnel(proxy,api_url,token,local,metadata(local))
                finally:
                    EDGE.unlink(missing_ok=True)
                STOP.wait(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start","serve","pause","resume"])
    args = parser.parse_args()
    os.chdir(ROOT)
    os.umask(0o077)
    DIRECTORY.mkdir(mode=0o700,parents=True,exist_ok=True)
    if args.action == "pause":
        PAUSE.touch()
    elif args.action == "resume":
        PAUSE.unlink(missing_ok=True)
    elif args.action == "start":
        if subprocess.run(["tmux","has-session","-t","="+SESSION],capture_output=True).returncode:
            subprocess.run(["tmux","new-session","-d","-s",SESSION,"-c",str(ROOT),
               'umask 077; while true; do .venv/bin/python scripts/gpu_agent.py serve >> logs/gpu-agent/agent.log 2>&1; sleep 3; done'],check=True)
    else:
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: STOP.set())
        serve()


if __name__ == "__main__":
    main()
