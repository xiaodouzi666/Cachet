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
    directory = os.open(ROOT, os.O_RDONLY)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
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
        existing = dotenv_values(target) if target.exists() else {}
        changed = False
        for key in ("ADMIN_TOKEN", "CACHET_SOAK_TOKEN"):
            if key == "ADMIN_TOKEN" and existing.get(key):
                continue
            if key == "CACHET_SOAK_TOKEN" and len(existing.get(key) or "") >= 32 and existing[key] != existing.get("ADMIN_TOKEN"):
                continue
            setting = key + "=" + secrets.token_urlsafe(48)
            pattern = re.compile(r"^[ \t]*(?:export[ \t]+)?" + key + r"[ \t]*=.*$", re.MULTILINE)
            if pattern.search(content):
                content = pattern.sub(setting, content)
            else:
                content = content.rstrip("\n") + "\n" + setting + "\n"
            changed = True
        if not changed:
            print("Using the existing project .env.")
            return
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
        print("Configured the private gateway and operator credentials in .env.")
    finally:
        os.close(directory)


if __name__ == "__main__":
    main()
