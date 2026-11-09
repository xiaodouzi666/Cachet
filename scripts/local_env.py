"""Create a project-local development environment without overwriting existing settings."""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / ".env"
    content = (
        (ROOT / ".env.example")
        .read_text()
        .replace("ADMIN_TOKEN=\n", "ADMIN_TOKEN=" + secrets.token_urlsafe(32) + "\n")
        .replace(
            "DATABASE_URL=sqlite:////var/data/cachet.db",
            "DATABASE_URL=sqlite:///./.data/cachet.db",
        )
    )
    try:
        fd = os.open(target, os.O_WRONLY | os.O_EXCL | os.O_CREAT, 0o600)
    except FileExistsError:
        print("Using the existing project .env.")
        return
    with os.fdopen(fd, "w") as handle:
        handle.write(content)
    print(
        "Created private .env with a development gateway token and local database path."
    )


if __name__ == "__main__":
    main()
