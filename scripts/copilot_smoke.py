"""Exercise the three judge questions in an existing sandbox workspace."""
import argparse
import json
import sys
from pathlib import Path

import httpx

from e2e_demo import admin_for_origin, origin
from sandbox_smoke import check
from backend.app.agents.copilot_tools import usd
from backend.app.paypal.client import PayPalClient, settings


def ask(client, question, expected_tool):
    messages = []
    event = None
    with client.stream("POST", "/api/copilot", json={"message": question}) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if event == "error":
                    raise RuntimeError(payload["message"])
                messages.append((event, payload))
    check(messages and messages[-1] == ("done", {"tool": expected_tool}),
          f"Expected grounded {expected_tool} output.")
    return messages


def run(args):
    config = settings()
    check(config.get("PAYPAL_ENV") == "sandbox", "Use the PayPal sandbox.")
    state = json.loads(args.state.read_text())
    base = origin(args.api)
    check(state["origin"] == base, "The journal belongs to a different backend.")
    if base.startswith("https://"):
        check(base == config.get("PUBLIC_API_URL"), "Expected the configured production API origin.")
    token = admin_for_origin(config, base)
    with httpx.Client(base_url=base, headers={"X-Admin-Token": token},
                      cookies={"cachet_workspace": state["workspace_cookie"]},
                      follow_redirects=False, timeout=180) as client:
        def get(path):
            return client.get("/api/" + path).raise_for_status().json()

        check(get("judge/state")["workspace_id"] == state["workspace_id"], "Workspace mismatch.")
        m = get("mandates/" + args.mandate)
        captures = [r for r in get("captures") if r["mandate_id"] == args.mandate]
        eligible = {r["id"]: r for r in captures if args.call in r["call_ids_json"]}
        check(eligible, "Select a paid call from this mandate with a remaining refundable allocation.")
        royalty = get("royalties")
        publishers = {row["publisher_id"] for row in royalty if row["hit_tokens"]}
        check(len(publishers) >= 2, "Select a workspace with earned royalties on both published layers.")
        money = ask(client, "How much did Starlette earn this week, and why more per token than FastAPI?", "publisher_earnings")
        answer = "".join(row["text"] for event, row in money if event == "delta")
        check("Shapley" in answer and all(p in answer for p in publishers) and "$" in answer,
              "Missing weekly earnings, publisher IDs or attribution explanation.")
        print(answer, flush=True)
        trace = ask(client, f"Where did the money from mandate {args.mandate} go?", "mandate_breakdown")
        answer = "".join(row["text"] for event, row in trace if event == "delta")
        check(args.mandate in answer and usd(m["captured_micros"]) in answer and
              any(c["paypal_capture_id"] in answer for c in captures), "Missing captured amount or payment IDs.")
        print(answer, flush=True)
        before = {r["paypal_refund_id"] for r in get("refunds")}
        messages = ask(client, f"Refund call {args.call}.", "propose_refund")
        proposals = [p for event, p in messages if event == "proposal"]
        check(len(proposals)==1 and proposals[0]["call_id"] == args.call and
              proposals[0]["mandate_id"] == args.mandate, "Missing scoped confirmation proposal.")
        check({r["paypal_refund_id"] for r in get("refunds")} == before, "The proposal changed provider receipts.")
        print(proposals[0]["description"], flush=True)
        if args.confirm:
            def confirm():
                return client.post("/api/copilot/confirm", json={
                    "proposal_id": proposals[0]["proposal_id"]
                }).raise_for_status().json()
            receipt = confirm()
            check(confirm() == receipt, "A repeated confirmation changed the receipt.")
            after = get("refunds")
            with PayPalClient(config) as pp:
                matches = [r for r in after if r["capture_id"] in eligible and args.call in r["call_ids_json"]
                           and r["paypal_refund_id"] not in before and r["reason"] == "dispute"]
                check(matches, "No new sandbox dispute refund for the selected captured call.")
                for r in matches:
                    remote = pp.get_refund(r["paypal_refund_id"])
                    check(remote["id"] == r["paypal_refund_id"] and remote["status"] == "COMPLETED" and
                          remote["amount"]["currency_code"] == "USD" and
                          remote["amount"]["value"] == f"{r['amount_cents']//100}.{r['amount_cents']%100:02}",
                          "PayPal refund readback differs from the workspace record.")
                    print(f"Confirmed sandbox refund {remote['id']} for USD {remote['amount']['value']}.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--state", required=True, type=Path, help="Private workspace journal.")
    parser.add_argument("--mandate", required=True)
    parser.add_argument("--call", required=True)
    parser.add_argument("--confirm", action="store_true", help="Confirm the proposed dispute in the sandbox.")
    try:
        run(parser.parse_args())
    except (OSError, ValueError, KeyError, RuntimeError, httpx.HTTPError) as exc:
        sys.exit(str(exc))
