"""Register the real Cachet receiver with the PayPal sandbox and verify its subscription."""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.paypal.client import PayPalClient, PayPalConfigError, PayPalError, request_id, settings
from backend.app.paypal.webhooks import EVENTS, delivery_evidence, replay_evidence


def ensure_registration(pp, url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provide a public HTTPS receiver URL without credentials, query, or fragment")
    existing = next((hook for hook in pp.list_webhooks() if hook["url"] == url), None)
    if not existing:
        pp.register_webhook(url, EVENTS, request_id("webhook-new", url, sorted(EVENTS)))
    else:
        old_names = {t["name"] for t in existing["event_types"]}
        if not EVENTS <= old_names and "*" not in old_names:
            pp.update_webhook_events(existing["id"], old_names | EVENTS,
                                     request_id("webhook-update", existing["id"], sorted(old_names | EVENTS)))
    found = next((hook for hook in pp.list_webhooks() if hook["url"] == url), {})
    names = {t["name"] for t in found.get("event_types", [])}
    if not found.get("id") or not (EVENTS <= names or "*" in names):
        raise RuntimeError("The subscription was not confirmed by PayPal GET")
    return {"webhook_id": found["id"], "url": url, "event_types": sorted(names)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Defaults to PUBLIC_API_URL/api/webhooks/paypal")
    parser.add_argument("--check", nargs=2, type=Path, metavar=("WALLET_JOURNAL", "PAYOUT_JOURNAL"),
                        help="Check verified receiver events against the two completed real smoke runs.")
    parser.add_argument("--wait", type=int, default=0, help="Seconds to wait for pending deliveries.")
    parser.add_argument("--redeliver", type=Path, metavar="E2E_EVIDENCE", help="Request signed redelivery of actual refund, capture and approval events, in reverse payment order.")
    args = parser.parse_args()
    if args.redeliver:
        try:
            print(json.dumps(replay_evidence(json.loads(args.redeliver.read_text()), args.wait or 180), indent=2))
        except (ValueError, PayPalConfigError, PayPalError, RuntimeError) as exc:
            parser.exit(1, str(exc) + "\n")
        raise SystemExit(0)
    if args.check:
        wallet, payout = (json.loads(p.read_text()) for p in args.check)
        deadline = time.monotonic() + args.wait
        while True:
            evidence = delivery_evidence(wallet, payout)
            if not evidence["missing"] or time.monotonic() >= deadline:
                print(json.dumps(evidence, indent=2))
                raise SystemExit(1 if evidence["missing"] else 0)
            time.sleep(3)
    public_api = settings().get("PUBLIC_API_URL")
    if not args.url and not public_api:
        parser.error("Configure PUBLIC_API_URL or pass --url for the public HTTPS webhook receiver.")
    try:
        with PayPalClient.from_env() as pp:
            result = ensure_registration(pp, args.url or public_api.rstrip("/") + "/api/webhooks/paypal")
        print(json.dumps(result, indent=2))
        print("Configure the returned PAYPAL_WEBHOOK_ID on the receiver.", file=sys.stderr)
    except (ValueError, PayPalConfigError, PayPalError, RuntimeError) as exc:
        parser.exit(1, f"{exc}\n")
