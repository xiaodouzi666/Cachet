"""Disconnect a real test tunnel to the live engine while observing an isolated Cachet API."""

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.config import value


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    original = urlsplit(value("SGLANG_BASE_URL", "http://127.0.0.1:30000"))
    if original.scheme != "http" or original.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise SystemExit("Run this diagnostic on the loopback GPU host.")
    if not value("SGLANG_API_KEY"):
        raise SystemExit("A live, authenticated GPU is required.")

    async def relay(reader, writer):
        try:
            peer, upstream = await asyncio.open_connection(
                original.hostname, original.port or 80
            )
        except OSError:
            writer.close()
            return

        async def copy(source, destination):
            try:
                while chunk := await source.read(65536):
                    destination.write(chunk)
                    await destination.drain()
            finally:
                destination.close()

        await asyncio.gather(
            copy(reader, upstream), copy(peer, writer), return_exceptions=True
        )

    forward = await asyncio.start_server(relay, "127.0.0.1", 0)
    tunnel_port = forward.sockets[0].getsockname()[1]
    # Pass the bound listener to Uvicorn to avoid any port-availability race.
    with socket.socket() as listener, tempfile.TemporaryDirectory() as scratch:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        api_port = listener.getsockname()[1]
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--fd",
            str(listener.fileno()),
            cwd=ROOT,
            pass_fds=[listener.fileno()],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={
                **os.environ,
                "SGLANG_BASE_URL": f"http://127.0.0.1:{tunnel_port}",
                "DATABASE_URL": f"sqlite:///{scratch}/probe.db",
            },
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{api_port}", timeout=3, trust_env=False
            ) as client:

                async def expect_mode(target):
                    started = time.monotonic()
                    while time.monotonic() - started < 30:
                        try:
                            response = await client.get("/api/health")
                            if (
                                response.is_success
                                and response.json().get("mode") == target
                            ):
                                return round(time.monotonic() - started, 3)
                        except httpx.RequestError:
                            pass
                        await asyncio.sleep(0.20)
                    raise RuntimeError(
                        f"The API did not reach {target} mode within 30 seconds"
                    )

                await expect_mode("live")
                forward.close()
                await forward.wait_closed()
                lost = await expect_mode("replay")
                response = await client.get(
                    "/api/reference-turn",
                    headers={"X-Admin-Token": value("ADMIN_TOKEN")},
                )
                archived = response.raise_for_status().json()
                if archived["mode"] != "recorded" or not archived["cached_tokens"]:
                    raise ValueError("Expected verified archived turn evidence")
                forward = await asyncio.start_server(relay, "127.0.0.1", tunnel_port)
                restored = await expect_mode("live")
                result = {
                    "transitions": ["live", "replay", "live"],
                    "disconnect_seconds": lost,
                    "reconnect_seconds": restored,
                    "archived_task": archived["task_id"],
                    "archived_turn": archived["turn"],
                    "archived_prompt_tokens": archived["prompt_tokens"],
                    "archived_cached_tokens": archived["cached_tokens"],
                }
                print(json.dumps(result))
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(result, indent=2) + "\n")
        finally:
            forward.close()
            await forward.wait_closed()
            child.terminate()
            await child.wait()


if __name__ == "__main__":
    asyncio.run(main())
