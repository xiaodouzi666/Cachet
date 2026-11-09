"""Exercise the PayPal sandbox with real money-movement APIs, then GET each resource."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.paypal.client import PayPalClient, PayPalConfigError, PayPalError, request_id, require, settings, usd


def now():
    return datetime.now(timezone.utc).isoformat()


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


class Journal:
    """Keep an atomic, private record of submitted operations and provider readbacks."""

    def __init__(self, path, scenario):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text())
            check(self.data["scenario"] == scenario, "Select the same scenario to resume this journal.")
        else:
            self.data = {"run_id": str(uuid.uuid4()), "scenario": scenario, "started": now(),
                         "operations": {}, "readbacks": {}, "completed": False}
            self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            json.dump(self.data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    def operation(self, name, fn, pick_id=lambda r: r["id"]):
        ops = self.data["operations"]
        if name not in ops:
            ops[name] = {"request_id": request_id(self.data["run_id"], name), "started": now()}
            self.save()
        op = ops[name]
        if "id" not in op:
            # Orders v2 request IDs are kept for six hours by default.
            age = datetime.now(timezone.utc) - datetime.fromisoformat(op["started"])
            check(age.total_seconds() < 6 * 3600, f"Review {name} in PayPal before retrying its expired request ID.")
            op["id"] = pick_id(fn(op["request_id"]))
            check(bool(op["id"]), f"No provider ID for {name}")
            self.save()
        return op["id"]

    def record(self, name, value):
        self.data["readbacks"][name] = {"observed": now(), **value}
        self.save()


@contextmanager
def locked_journal(path, scenario):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield Journal(path, scenario)


def reference(resource):
    return {key: resource[key] for key in ("id", "status", "amount", "final_capture") if key in resource}


def approval_link(order):
    return next((link["href"] for link in order.get("links", []) if link.get("rel") in ("approve", "payer-action")), None)


def approve_wallet(pp, order, receipt, config):
    """Use the configured buyer, then require server-side confirmation of approval."""
    if order.get("status") in ("APPROVED", "COMPLETED"):
        return order
    link = approval_link(order)
    check(link, "The sandbox did not return a buyer-approval link.")
    child_env = dict(os.environ)
    for name in ("JUDGE_BUYER_EMAIL", "JUDGE_BUYER_PASSWORD"):
        child_env[name] = require(config, name)
    if config.get("PLAYWRIGHT_CHROMIUM_CHANNEL"):
        child_env["PLAYWRIGHT_CHROMIUM_CHANNEL"] = config["PLAYWRIGHT_CHROMIUM_CHANNEL"]
    child = subprocess.Popen(["node", str(ROOT / "scripts/approve_buyer.ts"), link, order["id"], receipt], env=child_env)
    try:
        deadline = time.monotonic() + 170
        while time.monotonic() < deadline:
            current = pp.get_order(order["id"])
            if current.get("status") in ("APPROVED", "COMPLETED"):
                return current
            check(child.poll() in (None, 0), f"Sandbox wallet did not approve order {order['id']}.")
            time.sleep(2)
        raise RuntimeError(f"PayPal GET did not show approval for {order['id']}.")
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def authorization_from_order(order):
    auths = [a for unit in order.get("purchase_units", []) for a in unit.get("payments", {}).get("authorizations", [])]
    check(len(auths) == 1 and auths[0].get("id"), "Expected one authorization in the PayPal order.")
    return auths[0]["id"]


def mandate(pp, journal, label, config, card=False):
    receipt = (config.get("PUBLIC_WEB_URL") or "http://127.0.0.1:3000").rstrip("/") + "/agent"
    def make(key):
        return pp.create_order(500, key, card=card, return_url=receipt, cancel_url=receipt + "?cancel=1")
    order_id = journal.operation(label + ".order", make)
    order = pp.get_order(order_id)
    amount = order["purchase_units"][0]["amount"]
    check(order["intent"] == "AUTHORIZE" and all(amount.get(k) == v for k, v in usd(500).items()), "Order intent or amount differs from the request.")
    journal.record(label + ".order", {"id": order_id, "status": order["status"], "amount": amount})
    if not card:
        order = approve_wallet(pp, order, receipt, config)
        journal.record(label + ".buyer_approval", {"id": order_id, "status": order["status"]})
    if order.get("status") == "COMPLETED":
        auth_id = authorization_from_order(order)
    else:
        if card:
            check(order.get("status") != "PAYER_ACTION_REQUIRED", "Sandbox card requires buyer interaction.")
        auth_id = journal.operation(label + ".authorize", lambda key: pp.authorize_order(order_id, key),
                                    authorization_from_order)
    fetched_order = pp.get_order(order_id)
    check(authorization_from_order(fetched_order) == auth_id, "Order GET does not agree with the authorization ID.")
    auth = pp.get_authorization(auth_id)
    check(auth["id"] == auth_id and auth["amount"] == usd(500), "Authorization GET amount or ID mismatch.")
    journal.record(label + ".authorization", reference(auth))
    return auth_id


def capture(pp, journal, label, auth_id, cents, final=False, replay=False):
    def charge(key):
        return pp.capture(auth_id, cents, final, key)
    operation = label + ".capture"
    capture_id = journal.operation(operation, charge)
    if replay and operation + ".idempotent_replay" not in journal.data["readbacks"]:
        repeated = charge(journal.data["operations"][operation]["request_id"])
        check(repeated.get("id") == capture_id, "Replayed request returned a different capture ID.")
        journal.record(operation + ".idempotent_replay", {"id": capture_id})
    actual = pp.get_capture(capture_id)
    check(actual["amount"] == usd(cents), "GET capture amount mismatch.")
    check(actual.get("status") in ("COMPLETED", "PARTIALLY_REFUNDED", "REFUNDED"), "Capture has not completed.")
    check(actual.get("final_capture") is final, "GET final_capture differs from the requested value.")
    breakdown = actual.get("seller_receivable_breakdown", {})
    check(breakdown.get("paypal_fee", {}).get("currency_code") == "USD", "PayPal capture fee unavailable.")
    journal.record(operation, {**reference(actual), "seller_receivable_breakdown": breakdown,
                               "authorization": reference(pp.get_authorization(auth_id))})
    return capture_id


def void(pp, journal, label, auth_id):
    actual = pp.get_authorization(auth_id)
    if actual["status"] != "VOIDED":
        # A successful 204 has no resource body. GET confirms that the authorization changed.
        journal.operation(label + ".void", lambda key: pp.void(auth_id, key), lambda _: auth_id)
        actual = pp.get_authorization(auth_id)
    check(actual["status"] == "VOIDED", f"Authorization {auth_id} has not been voided.")
    journal.record(label + ".void", reference(actual))


def run_wallet(pp, journal, config):
    auth = mandate(pp, journal, "multi", config)
    first = capture(pp, journal, "multi.first", auth, 100, replay=True)
    second = capture(pp, journal, "multi.second", auth, 50)
    final = capture(pp, journal, "multi.final", auth, 30, final=True)
    check(len({first, second, final}) == 3, "Different partial captures returned duplicate PayPal IDs.")
    if "multi.release" not in journal.data["readbacks"]:
        actual = pp.get_authorization(auth)
        check(actual["status"] in ("PARTIALLY_CAPTURED", "CAPTURED", "CREATED", "VOIDED"),
              "Cannot establish the disposition of the unused authorization.")
        journal.record("multi.release", {**reference(actual), "links": [link["rel"] for link in actual["links"]],
                                         "explicit_void_needed": actual["status"] in ("CREATED", "PARTIALLY_CAPTURED")})
    if journal.data["readbacks"]["multi.release"]["explicit_void_needed"]:
        void(pp, journal, "multi", auth)
    refund_id = journal.operation("multi.refund", lambda key: pp.refund(second, 20, key))
    refund = pp.get_refund(refund_id)
    check(refund.get("status") == "COMPLETED" and refund["amount"] == usd(20), "Refund is incomplete or has the wrong amount.")
    refunded = pp.get_capture(second)
    check(refunded["status"] == "PARTIALLY_REFUNDED", "Capture GET did not confirm the partial refund.")
    journal.record("multi.refund", {**reference(refund), "capture": reference(refunded)})
    partial = mandate(pp, journal, "partial", config)
    capture(pp, journal, "partial.first", partial, 10)
    void(pp, journal, "partial", partial)
    void(pp, journal, "unused", mandate(pp, journal, "unused", config))


def run_payout(pp, journal, config):
    emails = [require(config, "PUBLISHER_STARLETTE_EMAIL"), require(config, "PUBLISHER_FASTAPI_EMAIL")]
    check(emails[0].casefold() != emails[1].casefold(), "Provide two distinct publisher receivers.")
    batch = request_id(journal.data["run_id"], "payout")
    items = [("starlette", emails[0], 10), ("fastapi", emails[1], 20)]
    batch_id = journal.operation("payout", lambda key: pp.create_payout(batch, items, key),
                                 lambda r: r["batch_header"]["payout_batch_id"])
    deadline = time.monotonic() + 180
    while True:
        result = pp.get_payout(batch_id)
        header = result["batch_header"]
        check(header["sender_batch_header"]["sender_batch_id"] == batch, "Payout sender batch ID changed.")
        statuses = {i["transaction_status"] for i in result.get("items", [])}
        journal.record("payout.batch", {"id": batch_id, "status": header["batch_status"], "fees": header.get("fees"),
                                        "item_statuses": sorted(statuses)})
        if header["batch_status"] == "SUCCESS" and statuses == {"SUCCESS"}:
            break
        check(header["batch_status"] in ("PENDING", "PROCESSING", "SUCCESS") and statuses <= {"PENDING", "SUCCESS"},
              "Payout was not credited to both receivers. Inspect the batch before retrying.")
        check(time.monotonic() < deadline, "Payout is still pending. Resume the same journal to check again.")
        time.sleep(3)
    check(len(result["items"]) == len(items), "Unexpected number of payout items.")
    expected = {item: (email, usd(cents)["value"]) for item, email, cents in items}
    for i in result["items"]:
        fetched = pp.get_payout_item(i["payout_item_id"])
        detail = fetched["payout_item"]
        sender = detail["sender_item_id"]
        check(sender in expected, "Unknown payout sender item.")
        email, value = expected.pop(sender)
        check(fetched["transaction_status"] == "SUCCESS" and detail["receiver"].casefold() == email.casefold()
              and detail["amount"] == {"currency":"USD", "value":value}, "Payout item GET does not match the request.")
        journal.record("payout." + sender, {"id": fetched["payout_item_id"], "transaction_id": fetched.get("transaction_id"),
                                           "status": fetched["transaction_status"], "amount": detail["amount"],
                                           "payout_item_fee": fetched.get("payout_item_fee")})


def run(scenario, journal_path):
    config = settings()
    with PayPalClient(config) as pp, locked_journal(journal_path, scenario) as journal:
        pp.access_token()
        if scenario in ("all", "wallet"):
            run_wallet(pp, journal, config)
        if scenario in ("all", "payout"):
            run_payout(pp, journal, config)
        if scenario == "approval":
            for number in range(1, 6):
                label = f"approval-{number}"
                void(pp, journal, label, mandate(pp, journal, label, config))
        if scenario == "card":
            void(pp, journal, "card", mandate(pp, journal, "card", config, card=True))
        journal.data["completed"] = True
        journal.save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=["wallet", "card", "payout", "approval", "all"])
    parser.add_argument("--journal", type=Path, help="Use the same private journal to resume after interruption.")
    args = parser.parse_args()
    os.umask(0o077)
    journal_path = args.journal or ROOT / "logs" / f"sandbox-{args.scenario}-{uuid.uuid4()}.json"
    print(f"Sandbox evidence journal: {journal_path}", flush=True)
    try:
        run(args.scenario, journal_path)
    except (PayPalError, PayPalConfigError, RuntimeError, ValueError, BlockingIOError) as exc:
        parser.exit(1, f"{exc}\n")
    print("All requested sandbox GET readbacks passed.")
