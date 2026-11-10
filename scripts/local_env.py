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
        values = dotenv_values(target) if target.exists() else {}
        missing = [key for key in ("ADMIN_TOKEN", "BFF_TOKEN") if not values.get(key)]
        if not missing:
            return
        for key in missing:
            setting = key + "=" + secrets.token_urlsafe(32)
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
        os.close(directory)


if __name__ == "__main__":
    main()
