"""Run official PayPal Agent Toolkit read tools in their own dependency environment."""

import json
import logging
import re
import subprocess
import sys
from functools import lru_cache
from urllib.parse import parse_qsl, urlsplit

from backend.app.paypal.client import ROOT, PayPalClient, PayPalConfigError, PayPalError, resource_id


def _worker(payload):
    executable = ROOT / ".venv-toolkit/bin/python"
    if not executable.exists():
        raise PayPalConfigError("Install the PayPal Agent Toolkit with python scripts/toolkit_probe.py install.")
    try:
        child = subprocess.run([str(executable), "-m", "backend.app.paypal.toolkit"], cwd=ROOT,
                               input=json.dumps(payload), text=True, capture_output=True, timeout=90)
    except subprocess.TimeoutExpired as exc:
        raise PayPalError(504, "TOOLKIT_TIMEOUT") from exc
    if child.returncode:
        raise PayPalError(502, "TOOLKIT_PROCESS_FAILED")
    result = json.loads(child.stdout)
    if "error" in result:
        raise PayPalError(**result["error"])
    return result["value"]


@lru_cache(maxsize=1)
def chat_tools():
    return _worker({"method": "definitions"})


def run_tool(name, arguments, *, order_ids, transaction_ids):
    """Both ID collections must come from the authenticated workspace database."""
    if name == "get_order_details":
        if arguments.get("order_id") not in set(order_ids):
            raise ValueError("The requested order is outside this workspace.")
        selected = [resource_id(arguments["order_id"])]
    elif name == "list_transactions":
        allowed = set(transaction_ids)
        requested = arguments.get("transaction_id")
        if requested and requested not in allowed:
            raise ValueError("The requested transaction is outside this workspace.")
        selected = [resource_id(requested)] if requested else sorted(resource_id(i) for i in allowed)
        if not selected:
            return {"transactions": []}
    else:
        raise ValueError("This PayPal tool is unavailable in the sandbox.")
    return _worker({"method": name, "arguments": arguments, "ids": selected})


def reporting_filters(params):
    filters = {k: v for k, v in params.items() if v is not None and v != "None"}
    # Toolkit 1.11.0 emits six fractional digits. PayPal Reporting accepts whole seconds.
    for field in ("start_date", "end_date"):
        if field in filters:
            filters[field] = re.sub(r"(\d{2}:\d{2}:\d{2})\.\d+", r"\1", filters[field])
    return filters


class ReportingTransport:
    """Use the real sandbox client behind the SDK's read handlers, restricting resource access."""

    def __init__(self, pp, order_id=None):
        self.pp = pp
        self.order_id = order_id
        self.error = None

    def get(self, uri, payload=None):
        path = urlsplit(uri)
        try:
            if self.order_id and path.path == f"/v2/checkout/orders/{self.order_id}":
                return self.pp.get_order(self.order_id)
            if path.path != "/v1/reporting/transactions" or path.netloc or path.scheme:
                raise ValueError("Unexpected SDK endpoint.")
            filters = reporting_filters(payload or dict(parse_qsl(path.query)))
            data = self.pp.list_transactions(filters)
            # In 1.11.0 the ID-search handler expects a JSON string. The list handler
            # expects a mapping and includes its absent optional filters as 'None'.
            return json.dumps(data) if payload is not None else data
        except PayPalError as exc:
            self.error = exc
            raise


def _execute(request):
    from importlib.metadata import version

    from paypal_agent_toolkit.openai.toolkit import PayPalToolkit
    from paypal_agent_toolkit.shared.configuration import Configuration, Context
    from paypal_agent_toolkit.shared.orders.tool_handlers import get_order_details
    from paypal_agent_toolkit.shared.transactions.tool_handlers import list_transactions

    if version("paypal-agent-toolkit") != "1.11.0":
        raise RuntimeError("Unexpected PayPal Agent Toolkit version")
    method = request["method"]
    if method == "definitions":
        config = Configuration(actions={"orders": {"get": True}, "transactions": {"list": True}},
                               context=Context(sandbox=True))
        return PayPalToolkit(None, None, config).get_openai_chat_tools()
    ids = request["ids"]
    with PayPalClient.from_env() as pp:
        if method == "get_order_details":
            order_id = ids[0]
            output = json.loads(get_order_details(ReportingTransport(pp, order_id), {"order_id": order_id}))["raw"]
            units = output["purchase_units"]
            return {"id": output["id"], "status": output["status"], "intent": output["intent"],
                    "amounts": [unit["amount"] for unit in units],
                    "authorizations": [a["id"] for u in units for a in u.get("payments", {}).get("authorizations", [])]}
        if method != "list_transactions":
            raise ValueError("Unsupported tool.")
        args = request["arguments"]
        sdk_args = {k: args[k] for k in ("start_date", "end_date", "page", "page_size", "transaction_status")
                    if args.get(k) is not None}
        if args.get("transaction_id"):
            sdk_args.update(transaction_id=ids[0], search_months=1)
        transport = ReportingTransport(pp)
        response = list_transactions(transport, sdk_args)
        if transport.error:
            raise transport.error
        if isinstance(response, str):
            response = json.loads(response)
        allowed = set(ids)
        visible = []
        for row in response.get("transaction_details", []):
            info = row["transaction_info"]
            if info.get("transaction_id") in allowed:
                visible.append({k: info[k] for k in ("transaction_id", "transaction_status", "transaction_amount",
                                                      "fee_amount", "transaction_initiation_date") if k in info})
        return {"transactions": visible}


if __name__ == "__main__":
    # Upstream handlers may print headers or account data when reporting an API error.
    import contextlib
    import os

    logging.disable(logging.CRITICAL)
    try:
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
            value = _execute(json.load(sys.stdin))
        reply = {"value": value}
    except PayPalError as exc:
        reply = {"error": {"status_code": exc.status_code, "name": exc.name,
                           "debug_id": exc.debug_id, "issue": exc.issue}}
    except PayPalConfigError:
        reply = {"error": {"status_code": 503, "name": "SANDBOX_NOT_CONFIGURED"}}
    print(json.dumps(reply))
