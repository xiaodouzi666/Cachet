"""Install or run a remotely managed Cloudflare named tunnel for the authenticated GPU."""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / ".tools/bin/cloudflared"
VERSION = "2026.10.7"
ASSETS = {
    "x86_64": (
        "amd64",
        "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872",
    ),
    "aarch64": (
        "arm64",
        "3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775",
    ),
}


def install():
    if platform.system() != "Linux" or platform.machine() not in ASSETS:
        raise SystemExit(
            "Install the pinned connector on a supported Linux GPU server."
        )
    arch, digest = ASSETS[platform.machine()]
    url = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/cloudflared-linux-{arch}"
    BINARY.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=BINARY.parent) as tmp:
        fetched = Path(tmp) / BINARY.name
        with urlopen(url, timeout=90) as remote, fetched.open("wb") as handle:
            shutil.copyfileobj(remote, handle)
        with fetched.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
                raise ValueError("Connector checksum differs from the release")
        fetched.chmod(0o755)
        fetched.replace(BINARY)
    subprocess.run([BINARY, "--version"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "start", "serve"])
    args = parser.parse_args()
    if args.action == "install":
        install()
        return
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    if not values.get("CLOUDFLARE_TUNNEL_TOKEN") or not values.get("SGLANG_API_KEY"):
        raise SystemExit(
            "Set the named tunnel token and the GPU API key in the server environment."
        )
    if not BINARY.is_file():
        raise SystemExit("Run this script with install first.")
    if args.action == "start":
        if (
            subprocess.run(
                ["tmux", "has-session", "-t", "=cachet-gpu-tunnel"], capture_output=True
            ).returncode
            == 0
        ):
            print("The GPU tunnel session already exists.")
            return
        # The connector obtains its token inside the child, never from its command line.
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                "cachet-gpu-tunnel",
                "-c",
                str(ROOT),
                sys.executable,
                str(Path(__file__).resolve()),
                "serve",
            ],
            check=True,
        )
        print(
            "Started the GPU tunnel session. Inspect logs/gpu-tunnel.log for connection status."
        )
        return
    os.umask(0o077)
    (ROOT / "logs").mkdir(exist_ok=True)
    with (ROOT / "logs/gpu-tunnel.log").open("a") as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
    os.environ["TUNNEL_TOKEN"] = values["CLOUDFLARE_TUNNEL_TOKEN"]
    os.execv(str(BINARY), [str(BINARY), "tunnel", "--no-autoupdate", "run"])


if __name__ == "__main__":
    main()
