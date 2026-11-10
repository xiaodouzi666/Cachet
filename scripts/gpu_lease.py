"""Serialize operations that measure or reset the same physical prefix cache."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def gpu_lease():
    path = Path(os.environ.get("CACHET_GPU_LEASE") or ROOT / ".cache" / "night-gpu.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
