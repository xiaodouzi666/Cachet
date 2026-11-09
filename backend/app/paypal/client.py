"""Authenticated transport for the PayPal sandbox. All monetary amounts are USD cents."""

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[3]
BASE_URL = "https://api-m.sandbox.paypal.com"
LOG = logging.getLogger(__name__)
_TOKEN_LOCK = threading.Lock()
_TOKENS = {}
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class PayPalConfigError(RuntimeError):
    """The sandbox application or the requested optional capability is unconfigured."""


class PayPalError(RuntimeError):
    def __init__(self, status_code, name, debug_id=None, issue=None, previous_payout=None):
        self.status_code = status_code
        self.name = name
        self.previous_payout = previous_payout
        self.debug_id = debug_id
        self.issue = issue
        description = " ".join(str(v) for v in (name, issue) if v)
        super().__init__(f"PayPal sandbox returned {status_code}: {description}"
                         + (f" (debug ID {debug_id})" if debug_id else ""))


def settings():
    return {**dotenv_values(ROOT / ".env"), **os.environ}


def require(config, name):
    if not config.get(name):
        raise PayPalConfigError(f"Configure {name} in the server environment.")
    return config[name]


def request_id(*parts):
    """Stable id for the same semantic operation, safe across client restarts."""
    if not parts:
        raise ValueError("An operation identity is required")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "cachet/paypal/" + json.dumps(parts, separators=(",", ":"))))


def resource_id(identifier):
    if not isinstance(identifier, str) or not ID_PATTERN.fullmatch(identifier):
        raise ValueError("Invalid PayPal resource identifier")
    return identifier


def usd(cents):
    if type(cents) is not int or cents <= 0:
        raise ValueError("A positive integer amount in cents is required")
    return {"currency_code": "USD", "value": f"{cents // 100}.{cents % 100:02d}"}


def retry_delay(response, attempt):
    value = response.headers.get("Retry-After")
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return 0.4 * 2 ** attempt


def provider_error(response):
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    details = data.get("details") or []
    issue = details[0].get("issue") if details and isinstance(details[0], dict) else None
    issue = issue if isinstance(issue, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,120}", issue) else None
    previous = None
    for section in [data, *details]:
        if not isinstance(section, dict):
            continue
        for link in section.get("links", []):
            if not isinstance(link, dict):
                continue
            match = re.fullmatch(r"https://api(?:-m)?\.sandbox\.paypal\.com/v1/payments/payouts/([A-Za-z0-9_-]{1,128})(?:\?[^#]*)?",
                                 link.get("href", ""))
            if match:
                previous = match[1]
    return PayPalError(response.status_code, data.get("name") or data.get("error") or "HTTP_ERROR",
                       response.headers.get("paypal-debug-id") or data.get("debug_id"), issue, previous)


class PayPalClient:
    def __init__(self, config):
        if config.get("PAYPAL_ENV", "sandbox") != "sandbox":
            raise PayPalConfigError("Cachet only accepts PAYPAL_ENV=sandbox.")
        self.client_id = require(config, "PAYPAL_CLIENT_ID")
        self._secret = require(config, "PAYPAL_CLIENT_SECRET")
        self._config = config
        self._http = httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(25, connect=8))
        self._app = hashlib.sha256((self.client_id + "\x00" + self._secret).encode()).digest()

    @classmethod
    def from_env(cls):
        return cls(settings())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._http.close()

    def _send(self, method, path, **kwargs):
        for attempt in range(4):
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise PayPalError(503, "SANDBOX_UNREACHABLE") from exc
                time.sleep(0.4 * 2 ** attempt)
                continue
            LOG.info("PayPal sandbox %s %s: HTTP %s, debug ID %s", method, path.split("?")[0],
                     response.status_code, response.headers.get("paypal-debug-id", "unavailable"))
            if attempt < 3 and (response.status_code == 429 or response.status_code >= 500):
                delay = retry_delay(response, attempt)
                if delay <= 8:
                    time.sleep(delay)
                    continue
            return response
        raise AssertionError("Unreachable retry state")

    def access_token(self):
        with _TOKEN_LOCK:
            token, expires = _TOKENS.get(self._app, (None, 0))
            if token and expires > time.monotonic():
                return token
            response = self._send("POST", "/v1/oauth2/token", auth=(self.client_id, self._secret),
                                  data={"grant_type": "client_credentials"})
            if not response.is_success:
                raise provider_error(response)
            token = response.json()
            _TOKENS[self._app] = (token["access_token"], time.monotonic() + max(0, token["expires_in"] - 60))
            return token["access_token"]

    def _call(self, method, path, key=None, body=None, params=None):
        headers = {"Accept": "application/json", "Prefer": "return=representation"}
        if method != "GET":
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,38}", key):
                raise ValueError("A stable PayPal request ID of at most 38 characters is required")
            headers["PayPal-Request-Id"] = key
            headers["Content-Type"] = "application/json"
        for attempt in range(2):
            token = self.access_token()
            headers["Authorization"] = "Bearer " + token
            response = self._send(method, path, headers=headers, json=body, params=params)
            if response.status_code == 401 and attempt == 0:
                with _TOKEN_LOCK:
                    if _TOKENS.get(self._app, (None,))[0] == token:
                        del _TOKENS[self._app]
                continue
            if not response.is_success:
                raise provider_error(response)
            return response.json() if response.content else {}
        raise AssertionError("Unreachable authentication state")

    def create_order(self, cents, key, *, card=False, return_url=None, cancel_url=None):
        body = {"intent": "AUTHORIZE", "purchase_units": [{"amount": usd(cents)}]}
        if card:
            number = require(self._config, "SANDBOX_TEST_CARD_NUMBER")
            expiry = require(self._config, "SANDBOX_TEST_CARD_EXPIRY")
            if not re.fullmatch(r"[0-9]{13,19}", number) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", expiry):
                raise PayPalConfigError("Check the configured sandbox card number and YYYY-MM expiry.")
            body["payment_source"] = {"card": {"number": number, "expiry": expiry}}
        elif return_url or cancel_url:
            context = {"shipping_preference": "NO_SHIPPING", "user_action": "PAY_NOW"}
            for name, url in (("return_url", return_url), ("cancel_url", cancel_url)):
                if url:
                    context[name] = url
            body["payment_source"] = {"paypal": {"experience_context": context}}
        return self._call("POST", "/v2/checkout/orders", key, body)

    def get_order(self, order_id):
        return self._call("GET", f"/v2/checkout/orders/{resource_id(order_id)}")

    def authorize_order(self, order_id, key):
        return self._call("POST", f"/v2/checkout/orders/{resource_id(order_id)}/authorize", key, {})

    def get_authorization(self, authorization_id):
        return self._call("GET", f"/v2/payments/authorizations/{resource_id(authorization_id)}")

    def capture(self, authorization_id, cents, final, key):
        return self._call("POST", f"/v2/payments/authorizations/{resource_id(authorization_id)}/capture",
                          key, {"amount": usd(cents), "final_capture": final, "invoice_id": key})

    def void(self, authorization_id, key):
        return self._call("POST", f"/v2/payments/authorizations/{resource_id(authorization_id)}/void", key)

    def get_capture(self, capture_id):
        return self._call("GET", f"/v2/payments/captures/{resource_id(capture_id)}")

    def refund(self, capture_id, cents, key):
        return self._call("POST", f"/v2/payments/captures/{resource_id(capture_id)}/refund",
                          key, {"amount": usd(cents), "custom_id": key})

    def get_refund(self, refund_id):
        return self._call("GET", f"/v2/payments/refunds/{resource_id(refund_id)}")

    def create_payout(self, batch_id, items, key):
        if not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", batch_id):
            raise ValueError("Invalid sender batch ID")
        payouts = []
        for sender_id, email, cents in items:
            amount = usd(cents)
            payouts.append({"recipient_type": "EMAIL", "receiver": email, "sender_item_id": sender_id,
                            "amount": {"currency": "USD", "value": amount["value"]}})
        if not 1 <= len(payouts) <= 1000 or len(set(p["sender_item_id"] for p in payouts)) != len(payouts):
            raise ValueError("Use 1 to 1000 distinct payout item IDs in a batch")
        try:
            return self._call("POST", "/v1/payments/payouts", key, {
                "sender_batch_header": {"sender_batch_id": batch_id, "email_subject": "Your Cachet publisher royalties"},
                "items": payouts,
            })
        except PayPalError as exc:
            if exc.status_code not in (400, 409, 422) or not exc.previous_payout:
                raise
            saved = self.get_payout(exc.previous_payout)
            header = saved["batch_header"]
            def identity(item):
                return (item["sender_item_id"], item["receiver"].casefold(), item["amount"]["currency"], item["amount"]["value"])
            if (header["sender_batch_header"]["sender_batch_id"] != batch_id or
                    sorted(identity(row["payout_item"]) for row in saved.get("items", [])) != sorted(map(identity, payouts))):
                raise
            return saved

    def get_payout(self, batch_id):
        return self._call("GET", f"/v1/payments/payouts/{resource_id(batch_id)}",
                          params={"page": 1, "page_size": 1000, "fields": "all"})

    def get_payout_item(self, item_id):
        return self._call("GET", f"/v1/payments/payouts-item/{resource_id(item_id)}")

    def verify_webhook(self, headers, event):
        hook_id = require(self._config, "PAYPAL_WEBHOOK_ID")
        incoming = {k.lower(): v for k, v in headers.items()}
        fields = {"auth_algo": "paypal-auth-algo", "cert_url": "paypal-cert-url",
                  "transmission_id": "paypal-transmission-id", "transmission_sig": "paypal-transmission-sig",
                  "transmission_time": "paypal-transmission-time"}
        if not all(incoming.get(k) for k in fields.values()) or not isinstance(event, dict):
            return False
        body = {field: incoming[header] for field, header in fields.items()}
        body.update(webhook_id=hook_id, webhook_event=event)
        key = request_id("verify", body["transmission_id"], body["transmission_sig"],
                         json.dumps(event, ensure_ascii=False, sort_keys=True))
        result = self._call("POST", "/v1/notifications/verify-webhook-signature", key, body)
        return result.get("verification_status") == "SUCCESS"

    def resend_webhook(self, event_id, key):
        hook_id = require(self._config, "PAYPAL_WEBHOOK_ID")
        return self._call("POST", f"/v1/notifications/webhooks-events/{resource_id(event_id)}/resend", key,
                          {"webhook_ids": [hook_id]})

    def list_webhooks(self):
        return self._call("GET", "/v1/notifications/webhooks").get("webhooks", [])

    def register_webhook(self, url, events, key):
        if urlsplit(url).scheme != "https" or not urlsplit(url).hostname:
            raise ValueError("PayPal requires a public HTTPS webhook URL")
        return self._call("POST", "/v1/notifications/webhooks", key,
                          {"url": url, "event_types": [{"name": name} for name in sorted(events)]})

    def update_webhook_events(self, webhook_id, events, key):
        return self._call("PATCH", f"/v1/notifications/webhooks/{resource_id(webhook_id)}", key,
                          [{"op": "replace", "path": "/event_types",
                            "value": [{"name": name} for name in sorted(events)]}])

    def list_transactions(self, filters):
        return self._call("GET", "/v1/reporting/transactions", params=filters)
