"""Initialize local development secrets while preserving existing project settings."""

import fcntl
import os
import re
import secrets
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".env"
    lock_fd = os.open(ROOT / ".env.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if target.exists():
            content = target.read_text()
        else:
            content = (
                (ROOT / ".env.example")
                .read_text()
                .replace(
                    "DATABASE_URL=sqlite:////var/data/cachet.db",
                    "DATABASE_URL=sqlite:///./.data/cachet.db",
                )
            )
        values = dotenv_values(target) if target.exists() else {}
        used = set()
        changes = {}
        for key in ("ADMIN_TOKEN", "BFF_TOKEN", "CACHET_SOAK_TOKEN"):
            current = values.get(key)
            if not current or current in used or (key == "CACHET_SOAK_TOKEN" and len(current) < 32):
                current = secrets.token_urlsafe(48)
                changes[key] = current
            used.add(current)
        if not changes:
            return
        for key, new_token in changes.items():
            setting = key + "=" + new_token
            pattern = re.compile(rf"^[ \t]*(?:export[ \t]+)?{key}[ \t]*=.*$", re.MULTILINE)
            if pattern.search(content):
                content = pattern.sub(setting, content)
            else:
                content = content.rstrip("\n") + "\n" + setting + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w", dir=ROOT, delete=False, prefix=".env."
        ) as handle:
            staged = Path(handle.name)
            try:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                staged.replace(target)
            finally:
                staged.unlink(missing_ok=True)
        print("Configured the private development gateway token in .env.")
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
