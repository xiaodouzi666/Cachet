"""Run a bounded, resumable live GPU soak against one dedicated $5 PayPal Sandbox mandate."""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.paypal.client import PayPalClient, require, settings
from e2e_demo import Demo, RateLimit, origin, save_report
from sandbox_smoke import check, locked_journal, now


def operator_request(api, token, method, path, body=None):
    route = "soak-registration" if path == "registration" else "soak/" + path
    response = api.request(method, "/api/operator/" + route, json=body, headers={"X-Cachet-Soak-Token":token})
    if response.status_code == 429:
        raise RateLimit(max(1, int(response.headers.get("Retry-After", "60"))), path)
    check(response.is_success, f"Soak API {method} {path} returned HTTP {response.status_code}. Resume the same journal.")
    return response.json()


def save_snapshot(journal, path, snapshot):
    receipt = {"environment": "PayPal Sandbox", "traffic": "Operator-run live GPU workload", "as_of": now(),
               "origin": journal.data["origin"], **snapshot}
    journal.data["latest"] = receipt
    journal.save()
    save_report(path, receipt)


def targets_met(s):
    return (s["threshold_captures"] >= 2 and s["fault_turns"] == 6 and bool(s["fault_refund_ids"])
            and len(s["publisher_available_micros"]) == 2 and min(s["publisher_available_micros"].values()) >= 250_000)


def run(args):
    cfg = settings()
    api_url = origin(args.api or require(cfg, "PUBLIC_API_URL"))
    if urlsplit(api_url).hostname not in ("127.0.0.1", "localhost", "::1"):
        check(api_url == origin(require(cfg, "PUBLIC_API_URL")), "Use the configured production API origin for the soak credential.")
    token = require(cfg, "CACHET_SOAK_TOKEN")
    admin = require(cfg, "ADMIN_TOKEN")
    check(len(token) >= 32 and token != admin, "The backend and the operator require an independent soak credential.")
    report = args.state.with_name(args.state.stem + "-evidence.json")
    with locked_journal(args.state, "operator-live-soak-v3") as journal, PayPalClient(cfg) as pp, httpx.Client(
        base_url=api_url, headers={"X-Admin-Token":admin}, timeout=httpx.Timeout(240, connect=8), follow_redirects=False,
    ) as api:
        check(journal.data.setdefault("origin", api_url) == api_url, "Resume the journal on its original backend.")
        demo = Demo(journal, api, pp, cfg, "card")
        if journal.data.get("workspace_cookie"):
            api.cookies.set("cachet_workspace", journal.data["workspace_cookie"])
        existing = journal.data.get("mandate_id")
        registration = operator_request(api, token, "GET", "registration")
        if registration["workspace_id"]:
            check(registration["workspace_id"] == journal.data.get("workspace_id") and
                  (not registration["mandate_id"] or existing == registration["mandate_id"]),
                  "This API already has a claimed recording workspace. Resume its original private journal.")
        if not existing:
            check(not args.status, "This private journal has no registered mandate.")
            paths = api.get("/openapi.json").raise_for_status().json()["paths"]
            check("/api/judge/capture-preview" in paths and "/api/operator/soak/reserve" in paths,
                  "Deploy fee-aware settlement and operator soak routes before opening the authorization.")
            demo.connect(api_url)
            context_id = demo.contexts["fastapi"]
            operator_request(api, token, "POST", "claim/" + context_id, {})
            state = demo.call("GET", "/judge/state")
            latest = state.get("mandate")
            if latest is None or (latest["status"] in {"CREATED", "APPROVED"} and latest["payment_method"] == "card"
                                  and latest["cap_micros"] == 5_000_000):
                latest = demo.call("POST", "/mandates/card", {"cap_usd":"5.00"})
            check(latest["status"] == "ACTIVE" and latest["payment_method"] == "card" and
                  latest["cap_micros"] == 5_000_000 and latest["authorization_id"], "Expected the dedicated active $5 card mandate.")
            auth = pp.get_authorization(latest["authorization_id"])
            order = pp.get_order(latest["paypal_order_id"])
            linked = [a["id"] for unit in order["purchase_units"] for a in unit.get("payments", {}).get("authorizations", [])]
            check(linked == [auth["id"]] and auth["status"] == "CREATED"
                  and auth["amount"] == {"currency_code":"USD", "value":"5.00"}, "PayPal did not confirm the dedicated open $5 hold.")
            journal.data.update(mandate_id=latest["id"], context_id=context_id)
            journal.save()
        mid = journal.data["mandate_id"]
        def get_state():
            state = operator_request(api, token, "GET", mid)
            check(state["workspace_id"] == journal.data["workspace_id"], "The reserved soak belongs to another workspace.")
            return state
        if args.status:
            print(json.dumps(get_state(), indent=2))
            return
        if not registration["mandate_id"]:
            operator_request(api, token, "POST", "reserve", {"mandate_id":mid, "context_id":journal.data["context_id"]})
        state = get_state()
        save_snapshot(journal, report, state)
        if args.continue_if_needed and not targets_met(state) and (
            time.time() >= state["until"] or state["billable_micros"] >= state["target_micros"]
        ):
            state = operator_request(api, token, "POST", f"{mid}/continue/{state['extensions'] + 1}", {})
        while True:
            save_snapshot(journal, report, state)
            check(state["status"] == "ACTIVE", "The recorded mandate is no longer active. Inspect it before continuing.")
            if state["last_run_status"] in ("queued", "running"):
                time.sleep(4)
            elif state["billable_micros"] >= state["target_micros"] or time.time() >= state["until"]:
                break
            elif demo.call("GET", "/health")["mode"] != "live":
                time.sleep(15)
            elif state["last_run_status"] == "failed":
                failed = journal.data.setdefault("failed_runs", [])
                if state["last_run_id"] not in failed:
                    failed.append(state["last_run_id"])
                    journal.save()
                check(len(failed) <= 3, "Three GPU run failures require inspection before resuming the same mandate.")
                time.sleep(4)
                post_step(api, token, journal, mid, state)
            else:
                post_step(api, token, journal, mid, state)
            state = get_state()
            if state["captured_fault_turns"] and not state["fault_refund_ids"]:
                # The real capture now includes delayed public hits. Use the normal judge audit.
                demo.call("POST", "/judge/audit", {"mandate_id":mid})
                state = get_state()
        auth = pp.get_authorization(state["authorization_id"])
        check(auth["status"] in {"CREATED", "PARTIALLY_CAPTURED"}, "PayPal did not confirm an open recording authorization.")
        captures = [pp.get_capture(cid) for cid in state["capture_ids"]]
        refunds = [pp.get_refund(fid) for fid in state["refund_ids"]]
        state = {**state, "authorization_get":auth["status"],
                 "capture_readbacks":[{"id":c["id"], "status":c["status"], "amount":c["amount"],
                    "fee":c["seller_receivable_breakdown"]["paypal_fee"], "net":c["seller_receivable_breakdown"]["net_amount"]}
                    for c in captures],
                 "refund_readbacks":[{"id":f["id"], "status":f["status"], "amount":f["amount"]} for f in refunds],
                 "recording_targets_met":bool(targets_met(state)), "reconciliation":demo.reconcile()}
        save_snapshot(journal, report, state)
        print(f"Recording mandate {mid} remains ACTIVE after {state['metered_turns']} metered turns. Evidence: {report}")


def post_step(api, token, journal, mid, state):
    sequence = state["sequence"] + 1
    try:
        receipt = operator_request(api, token, "POST", f"{mid}/runs", {"sequence": sequence})
    except httpx.TransportError:
        # Read back the server sequence before retrying a lost response.
        time.sleep(2)
        return
    except RateLimit as limited:
        time.sleep(max(0, min(limited.delay + 1, state["until"] - time.time())))
        return
    journal.data["last_submission"] = {"sequence":sequence, "run_id":receipt["run_id"], "at":now()}
    journal.save()


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", help="Configured production API or a loopback development API.")
    parser.add_argument("--state", type=Path, default=ROOT / "logs" / "soak-production.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Print only the sanitized current registration and receipts.")
    mode.add_argument("--continue-if-needed", action="store_true", help="Continue a spent window if recording targets remain, retaining $0.25 headroom.")
    try:
        run(parser.parse_args())
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
        parser.exit(1, str(exc) + "\n")
