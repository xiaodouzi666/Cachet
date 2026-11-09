"""Expose only the BFF settings and public SDK settings to the local Next process."""

import os
import sys
from pathlib import Path
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
values = dotenv_values(root / ".env")
if values.get("AG_STUDIO_LICENSE_KEY"):
    os.environ.setdefault(
        "NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY", values["AG_STUDIO_LICENSE_KEY"]
    )
for key in (
    "ADMIN_TOKEN",
    "CACHET_API_URL",
    "PUBLIC_API_URL",
    "PUBLIC_WEB_URL",
    "CACHET_TRUSTED_CLIENT_IP_HEADER",
    "NEXT_PUBLIC_PAYPAL_CLIENT_ID",
    "NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY",
):
    if values.get(key):
        os.environ.setdefault(key, values[key])
os.execvp(sys.argv[1], sys.argv[1:])
