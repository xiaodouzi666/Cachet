"""Launch the project inference service and redact secrets from its combined output."""
import json
import runpy
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def main() -> int:
    if sys.argv[1:2] == ["--check-model"]:
        model = Path(sys.argv[2])
        try:
            index = json.loads((model / "model.safetensors.index.json").read_text())
            shards = set(index["weight_map"].values())
            files = [model / shard for shard in shards]
            if not files or any(not path.is_file() or path.stat().st_size == 0 for path in files):
                raise ValueError("empty model shards")
        except (OSError, ValueError, KeyError) as exc:
            print(f"Incomplete model download ({exc}).", file=sys.stderr)
            return 1
        return 0

    from dotenv import dotenv_values

    config = dotenv_values(".env")
    key = config.get("SGLANG_API_KEY")
    if not key or not key.strip():
        raise SystemExit("Set SGLANG_API_KEY to a random secret in .env.")

    if sys.argv[1:] == ["--engine"]:
        url = urlsplit(config.get("SGLANG_BASE_URL") or "http://127.0.0.1:30000")
        if url.scheme != "http" or url.hostname != "127.0.0.1":
            raise SystemExit("The local inference service requires a loopback HTTP address.")
        # Add the secret in-process so the host command line contains no credentials.
        sys.argv = [
            "sglang.launch_server",
            "--model-path", ".cache/models/Qwen2.5-7B-Instruct-AWQ",
            "--served-model-name", "Qwen/Qwen2.5-7B-Instruct-AWQ",
            "--tool-call-parser", "qwen25",
            "--context-length", "32768",
            "--host", url.hostname,
            "--port", str(url.port or 80),
            "--api-key", key,
            "--mem-fraction-static", config.get("SGLANG_MEM_FRACTION") or "0.85",
            "--attention-backend", "triton",
            "--enable-cache-report",
        ]
        runpy.run_module("sglang.launch_server", run_name="__main__", alter_sys=True)
        return 0

    log_key = key.encode()
    with subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve()), "--engine"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as child:
        for line in child.stdout:
            sys.stdout.buffer.write(line.replace(log_key, b"[redacted]"))
            sys.stdout.buffer.flush()
        return child.wait()


if __name__ == "__main__":
    sys.exit(main())
