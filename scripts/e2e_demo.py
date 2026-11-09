"""Run Cachet's acceptance flow against a live GPU and real PayPal sandbox accounts."""

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from sandbox_smoke import ROOT, PayPalClient, approve_wallet, check, locked_journal, now, require, settings

SOURCES = ("starlette", "fastapi")
# Preparation earns real publisher balances with short, read-only framework tasks.
# The acceptance sessions always use the built-in coding task and its full contract.
PREPARATION_TASKS = (
    "Trace dependency resolution in FastAPI and show a minimal request-header dependency.",
    "Explain how a FastAPI route dispatches into Starlette and show a small ASGI example.",
    "Trace FastAPI exception handling through Starlette and demonstrate a custom exception handler.",
)
SETTLED = {"CLOSED", "VOIDED", "EXPIRED"}
DEFAULT_STATE = ROOT / "logs" / "demo-workspace.json"


def origin(url):
    parsed = urlsplit(url)
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))):
        raise ValueError("Use an HTTPS backend origin, or a loopback HTTP origin for local development.")
    return url.rstrip("/")


def save_report(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


class Demo:
    def __init__(self, journal, api, pp, config, payment, mode="live"):
        self.journal = journal
        self.data = journal.data
        self.api = api
        self.pp = pp
        self.cfg = config
        self.payment = payment
        self.mode = mode
        self.sessions = self.data.setdefault("sessions", [])
        self.publishers = {}
        self.contexts = {}

    def call(self, method, route, body=None):
        response = self.api.request(method, "/api" + route, json=body, timeout=1200 if route == "/reconcile" else 240)
        if response.status_code == 429:
            delay = int(response.headers.get("Retry-After", 60))
            raise RateLimit(max(1, delay), route)
        check(response.is_success, f"Backend {method} {route} returned HTTP {response.status_code}. Resume with the same state after inspecting the backend.")
        return response.json()

    def connect(self, api_url):
        check(self.data.setdefault("origin", api_url) == api_url, "Resume this workspace on its original backend.")
        if self.data.get("workspace_cookie"):
            self.api.cookies.set("cachet_workspace", self.data["workspace_cookie"])
            state = self.call("GET", "/judge/state")
            check(self.data["workspace_id"] == state["workspace_id"] and state["kind"] == "judge",
                  "The journal's original judge workspace is required.")
            # An interrupted GPU must not prevent closing an existing sandbox authorization.
            active = next((s for s in self.sessions if not s.get("finished")), None)
            last = state.get("run")
            if active and active.get("failure"):
                self.close_failed_run(active)
            if (active and last and last["status"] == "failed" and active.get("mandate_id") == last["mandate_id"]
                    and last["id"] == active.get("run_id", last["id"])):
                active["run_id"] = last["id"]
                active.setdefault("accepted_at", time.time())
                self.session_turns(active)
        health = self.call("GET", "/health")
        check(health.get("service") == "cachet-api" and health.get("mode") == self.mode and health.get("gpu"),
              f"Expected Cachet inference mode {self.mode}. Review /api/health.")
        check(health.get("paypal_configured") is True, "The backend requires a configured PayPal sandbox app.")
        if self.payment == "wallet":
            for field in ("JUDGE_BUYER_EMAIL", "JUDGE_BUYER_PASSWORD"):
                require(self.cfg, field)
        else:
            check(health.get("card_enabled"), "The backend has no fixed sandbox test card configured.")
        emails = [require(self.cfg, f"PUBLISHER_{slug.upper()}_EMAIL") for slug in SOURCES]
        check(len(set(x.casefold() for x in emails)) == len(SOURCES), "Provide two distinct publisher recipients.")
        self.pp.access_token()
        if not self.data.get("workspace_cookie"):
            state = self.call("POST", "/judge/workspace", {"kind":"judge"})
            self.data.update(workspace_id=state["workspace_id"], workspace_cookie=self.api.cookies["cachet_workspace"])
            self.journal.save()
        check(state["kind"] == "judge", "Use a dedicated judge workspace for operator test traffic.")
        self.data["health"] = {key: health.get(key) for key in ("service", "version", "mode", "gpu", "model", "build")}
        warm = {}
        for slug, email in zip(SOURCES, emails):
            publisher = self.call("POST", "/publishers", {"name":slug.capitalize() + " maintainer", "paypal_email":email})
            self.publishers[slug] = publisher["id"]
            ctx = self.call("POST", "/contexts", {"publisher_id":publisher["id"], "source_slug":slug,
                                                 "parent_id":self.contexts.get("starlette") if slug == "fastapi" else None})
            self.contexts[slug] = ctx["id"]
            # Demand a new real warm probe, including when resuming a previously hot context.
            self.call("POST", f"/contexts/{ctx['id']}/warm")
            deadline = time.monotonic() + 240
            while True:
                ctx = self.call("GET", f"/contexts/{ctx['id']}")
                if ctx["status"] == "hot" and (ctx.get("last_probe_hit_ratio") or 0) >= .95 and ctx.get("last_probe_at"):
                    break
                check(time.monotonic() < deadline, f"No confirmed warm probe for {slug}.")
                time.sleep(1)
            warm[slug] = {key:ctx[key] for key in ("id", "publisher_id", "token_len", "last_probe_hit_ratio", "last_probe_at")}
        self.data["warm"] = warm
        self.journal.save()

    def payable(self):
        result = {p:0 for p in self.publishers.values()}
        by_account = {f"publisher:{p}:payable":p for p in result}
        for entry in self.call("GET", "/ledger"):
            publisher = by_account.get(entry["account"])
            if publisher:
                result[publisher] += entry["credit_micros"] - entry["debit_micros"]
        return result

    def reconcile(self):
        result = self.call("GET", "/reconcile")
        check(result["ok"] is True and result["checked"] > 0 and not result["differences"],
              "PayPal GET reconciliation is empty or differs from the ledger.")
        return result

    def wait_for_slot(self):
        starts = sorted(s["accepted_at"] for s in self.sessions if s.get("accepted_at"))
        if len(starts) >= 3:
            wait = starts[-3] + 3602 - time.time()
            if wait > 0:
                print(f"Workspace rate limit: resuming in {int(wait) + 1} seconds.", flush=True)
                time.sleep(wait)

    def turns(self, run_id):
        rows = []
        terminal = False
        mode = decision = None
        with self.api.stream("GET", f"/api/agent/runs/{run_id}/events", timeout=httpx.Timeout(90, connect=10)) as response:
            check(response.is_success, f"Could not read events for run {run_id}.")
            event = None
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line.partition(":")[2].strip()
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line.partition(":")[2].strip())
                if event == "error":
                    raise FailedRun(payload["message"], rows)
                if event == "queued":
                    mode = payload["mode"]
                elif event == "decision":
                    decision = payload["decision"]
                elif event == "turn":
                    rows.append({key:payload[key] for key in ("id", "turn", "prompt_tokens", "cached_tokens", "output_tokens",
                                "ttft_ms", "measured_ttft_ms", "charge_micros", "royalty_json", "fault_injected_ms", "sla_breach", "mode")})
                elif event == "done":
                    terminal = payload["run_id"] == run_id and payload["status"] == "completed"
        check(terminal and mode == self.mode and decision == "buy", f"No completed live Buy run for {run_id}.")
        check(len(rows) >= 10 and len({r["turn"] for r in rows}) == len(rows) == len({r["id"] for r in rows})
              and all(r["mode"] == self.mode and r["prompt_tokens"] > 0 and r["output_tokens"] > 0 for r in rows),
              "Expected at least ten distinct live metered turns.")
        check(all(any(r["royalty_json"].get(p, 0) > 0 for r in rows) for p in self.publishers.values()),
              "Real metering must attribute positive royalties to each published layer.")
        return rows

    def session_turns(self, s):
        try:
            return self.turns(s["run_id"])
        except FailedRun as exc:
            s.update(failure={"at":now(), "reason":str(exc)}, turns=exc.rows)
            suite = self.data.get("acceptance_suite")
            if s["purpose"] == "acceptance" and suite and not suite.get("completed"):
                suite["initial"] = sum(1 for attempt in self.sessions if attempt.get("accepted"))
            self.journal.save()
        self.close_failed_run(s)

    def close_failed_run(self, s):
        mid = s["mandate_id"]
        mandate = self.call("GET", f"/mandates/{mid}")
        auth = mandate.get("authorization_id")
        check(auth and auth == s.get("authorization_id", auth) and mandate["paypal_order_id"] == s["order_id"],
              f"Inspect mandate {mid}: its sandbox order or authorization differs from this journal.")
        s["authorization_id"] = auth
        self.journal.save()
        # Audit committed calls first so uncaptured SLA reversals are applied before final collection.
        s["audit"] = self.call("POST", "/judge/audit", {"mandate_id":mid})
        closed = self.call("POST", f"/mandates/{mid}/close")
        check(closed["status"] in SETTLED and closed["authorization_id"] == auth,
              f"Inspect mandate {mid}: its sandbox authorization has not reached a verified terminal state.")
        s["closed"] = closed
        self.journal.save()
        s["reconciliation"] = self.reconcile()
        s["finished"] = now()
        self.journal.save()
        raise RuntimeError(f"Run {s['run_id']} failed. Mandate {mid} is {closed['status']} and reconciled. "
                           "Resolve the run error and rerun with the same journal to start another attempt.")

    def session(self, purpose):
        active = next((s for s in self.sessions if not s.get("finished")), None)
        if active is not None:
            check(active["purpose"] == purpose, f"First resume the unfinished {active['purpose']} session.")
            s = active
            if s.get("failure"):
                self.close_failed_run(s)
        else:
            s = {"purpose":purpose, "started":now(), "payable_before":self.payable()}
            self.sessions.append(s)
            self.journal.save()
        print(f"Running {purpose} session {self.sessions.index(s) + 1} in the persisted test workspace.", flush=True)
        if not s.get("mandate_id"):
            self.wait_for_slot()
            body = {"cap_usd": "5.00"}
            route = "/mandates/card" if self.payment == "card" else "/mandates"
            mandate = self.call("POST", route, body)
            s.update(mandate_id=mandate["id"], order_id=mandate["paypal_order_id"])
            self.journal.save()
        mid = s["mandate_id"]
        mandate = self.call("GET", f"/mandates/{mid}")
        if not mandate.get("authorization_id"):
            order = self.pp.get_order(s["order_id"])
            receipt = (self.cfg.get("PUBLIC_WEB_URL") or "http://127.0.0.1:3000").rstrip("/") + "/agent"
            approve_wallet(self.pp, order, receipt, self.cfg)
            mandate = self.call("POST", f"/mandates/{mid}/authorize")
        check(mandate["authorization_id"], "A real sandbox authorization is required.")
        s["authorization_id"] = mandate["authorization_id"]
        self.journal.save()
        if purpose == "seed" and not s.get("seed_reserved"):
            reserved = self.call("POST", f"/operator/seed-mandates/{mid}")
            check(reserved["reserved_mandate_id"] == mid, "Seed reservation refers to a different mandate.")
            s["seed_reserved"] = True
            self.journal.save()
        if not s.get("run_id"):
            # A previous process may have accepted the POST and lost its response.
            last = self.call("GET", "/judge/state").get("run")
            if last and last["mandate_id"] == mid:
                s.update(run_id=last["id"], accepted_at=time.time())
            else:
                check(mandate["status"] == "ACTIVE", "This mandate closed before a run started. Inspect its sandbox authorization.")
                if purpose == "acceptance":
                    self.call("POST", "/judge/fault", {"ttft_delay_ms":1200, "calls":3})
                workload = {"task_id": "rate-limit"}
                if purpose == "preparation":
                    index = sum(p["purpose"] == "preparation" for p in self.sessions[:self.sessions.index(s)])
                    workload = {"task_id": "custom", "task_text": PREPARATION_TASKS[index % len(PREPARATION_TASKS)]}
                s["workload"] = workload
                self.journal.save()
                try:
                    run = self.call("POST", "/agent/runs", {"mandate_id":mid, "context_id":self.contexts["fastapi"],
                                    **workload, "cold_fraction":.25, "est_turns":10})
                except RateLimit as exc:
                    # Release the unused authorization before waiting beyond the idle timeout.
                    voided = self.call("POST", f"/mandates/{mid}/close")
                    check(voided["status"] == "VOIDED" and not voided["captured_micros"], "Unused authorization was not released.")
                    s.setdefault("unused_authorizations", []).append(s.pop("authorization_id"))
                    s.pop("mandate_id"); s.pop("order_id"); s.pop("seed_reserved", None)
                    self.journal.save()
                    raise exc
                s.update(run_id=run["run_id"], accepted_at=time.time())
            self.journal.save()
        rows = self.session_turns(s)
        faults = [r for r in rows if r["fault_injected_ms"]]
        if purpose == "acceptance":
            check(len(faults) == 3 and all(r["sla_breach"] and r["ttft_ms"] >= 1200 for r in faults),
                  "Expected three observed delays that breached the SLA.")
        s["turns"] = rows
        self.journal.save()
        if mandate["status"] not in SETTLED:
            self.call("POST", "/judge/capture-now", {"mandate_id":mid})
        captured = [c for c in self.call("GET", "/captures") if c["mandate_id"] == mid]
        # For a final-only sandbox app, release the remainder before auditing captured money.
        if not captured and mandate["status"] not in SETTLED:
            mandate = self.call("POST", f"/mandates/{mid}/close")
            captured = [c for c in self.call("GET", "/captures") if c["mandate_id"] == mid]
            s["audit_after_final_close"] = True
        check(captured and all(c["paypal_capture_id"] and c["amount_cents"] > 0 for c in captured), "No confirmed capture for this session.")
        self.call("POST", "/judge/audit", {"mandate_id":mid})
        closed = self.call("POST", f"/mandates/{mid}/close")
        check(closed["status"] == "CLOSED" and 0 <= closed["refunded_micros"] <= closed["captured_micros"] <= closed["cap_micros"],
              "Closed mandate totals are inconsistent.")
        captures = [c for c in self.call("GET", "/captures") if c["mandate_id"] == mid]
        receipts = [r for r in self.call("GET", "/refunds") if r["capture_id"] in {c["id"] for c in captures}]
        if purpose == "acceptance":
            affected = {rid for r in receipts if r["status"] == "COMPLETED" and r["amount_cents"] > 0 and r["paypal_refund_id"]
                        for rid in r["call_ids_json"]}
            check({r["id"] for r in faults} <= affected, "The three injected calls are not linked to completed PayPal refunds.")
        if purpose == "seed":
            confirmed = self.call("POST", f"/operator/seed-runs/{s['run_id']}")
            check(confirmed["operator_seed_run_id"] == s["run_id"] and confirmed["operator_seeded"] is True,
                  "The completed operator seed was not confirmed.")
            s["operator_seeded"] = True
        s.update(captures=captures, refunds=receipts, closed=closed, payable_after=self.payable(), finished=now())
        self.journal.save()
        return s

    def complete_session(self, purpose):
        while True:
            try:
                return self.session(purpose)
            except RateLimit as exc:
                print(f"Backend rate limit on {exc.route}. Respecting Retry-After ({exc.delay}s).", flush=True)
                time.sleep(exc.delay + 1)

    def settlement(self, target):
        if "existing_batches" not in target:
            target["existing_batches"] = [b["id"] for b in self.call("GET", "/payouts")]
            self.journal.save()
        # If the response was lost, discover the just-created batch before initiating another settlement.
        candidates = [b for b in self.call("GET", "/payouts") if b["id"] not in target["existing_batches"]]
        check(len(candidates) <= 1, "Multiple new payout batches require operator inspection.")
        if candidates:
            batch = candidates[0]
        else:
            batch = self.call("POST", "/judge/settle")
        check(batch.get("id"), "The workspace has no eligible payout batch.")
        target["local_batch_id"] = batch["id"]
        self.journal.save()
        deadline = time.monotonic() + 240
        while True:
            self.call("POST", "/judge/settle", {"batch_id":batch["id"]})
            paid = next(b for b in self.call("GET", "/payouts") if b["id"] == batch["id"])
            items = paid["items"]
            if paid["status"] == "SUCCESS" and items and all(i["status"] == "SUCCESS" for i in items):
                break
            check(paid["status"] in {"CREATED", "PENDING", "PROCESSING", "SUCCESS"} and
                  all(i["status"] in {"PENDING", "SUCCESS"} for i in items),
                  "An unsuccessful payout item requires inspection at PayPal.")
            check(time.monotonic() < deadline, "The payout is still processing. Resume the same state.")
            time.sleep(5)
        check(paid["paypal_batch_id"], "PayPal did not return a batch ID.")
        check(len(paid["items"]) == 2 and {i["publisher_id"] for i in paid["items"]} == set(self.publishers.values()) and
              all(i["amount_cents"] >= 1 and i["paypal_item_id"] for i in paid["items"]),
              "Both publisher recipients must have successful identified payout items.")
        target["payout"] = paid
        target["reconciliation"] = self.reconcile()
        target["accepted"] = now()
        self.journal.save()

    def acceptance(self, repeats):
        # Start with enough pre-existing real earnings that refunds cannot strand one recipient below a cent.
        accepted = lambda: sum(1 for s in self.sessions if s.get("accepted"))
        suite = self.data.get("acceptance_suite")
        if not suite or suite.get("completed"):
            suite = {"started":now(), "initial":accepted(), "repeats":repeats}
            self.data["acceptance_suite"] = suite
            self.journal.save()
        check(repeats == suite["repeats"], "Resume the suite using its original repeat count.")
        while accepted() < suite["initial"] + repeats:
            unfinished = pending_acceptance(self.sessions)
            if unfinished is None:
                minimum = max(10000, int(Decimal(self.cfg.get("JUDGE_PAYOUT_MIN_USD") or ".01") * 1000000))
                if any(s["purpose"] == "preparation" and not s.get("finished") for s in self.sessions):
                    self.complete_session("preparation")
                for _ in range(30):
                    if min(self.payable().values()) >= minimum:
                        break
                    self.complete_session("preparation")
                check(min(self.payable().values()) >= minimum, "Two real balances did not reach the configured payout minimum in 30 preparation sessions.")
                unfinished = self.complete_session("acceptance")
            elif not unfinished.get("finished"):
                unfinished = self.complete_session("acceptance")
            self.settlement(unfinished)
        suite["completed"] = now()
        self.journal.save()


def pending_acceptance(sessions):
    return next((s for s in sessions if s["purpose"] == "acceptance" and not s.get("accepted")
                 and not (s.get("failure") and s.get("finished"))), None)


class FailedRun(RuntimeError):
    def __init__(self, message, rows):
        self.rows = rows
        super().__init__(message)


class RateLimit(RuntimeError):
    def __init__(self, delay, route):
        self.delay, self.route = delay, route
        super().__init__(f"Rate limited on {route}. Retry after {delay}s.")


def execute(args, action):
    os.umask(0o077)
    config = settings()
    payment = args.payment or config.get("E2E_PAYMENT") or "wallet"
    check(payment in {"wallet", "card"}, "E2E_PAYMENT must be wallet or card.")
    url = origin(args.api or config.get("PUBLIC_API_URL") or "http://127.0.0.1:8000")
    token = require(config, "ADMIN_TOKEN")
    with PayPalClient(config) as pp, httpx.Client(base_url=url, headers={"X-Admin-Token":token},
                follow_redirects=False, timeout=httpx.Timeout(240, connect=10)) as api, locked_journal(args.state, "operator-demo") as state:
        demo = Demo(state, api, pp, config, payment, getattr(args, "require_mode", "live"))
        demo.connect(url)
        action(demo)
        summary = {k:state.data[k] for k in ("workspace_id", "health", "warm", "sessions")}
        summary.update(traffic="Operator-run sandbox test traffic", generated_at=now())
        if state.data.get("acceptance_suite"):
            summary["acceptance_suite"] = state.data["acceptance_suite"]
        if state.data.get("seed"):
            summary["seed"] = state.data["seed"]
        if state.data.get("smoke"):
            summary["smoke"] = state.data["smoke"]
        report = args.state.with_name(args.state.stem + "-evidence.json")
        save_report(report, summary)
        print(f"Real test evidence: {report}")


def arguments(parser):
    parser.add_argument("--api", help="Backend origin. Defaults to PUBLIC_API_URL or the local backend.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Private, resumable workspace state under logs/.")
    parser.add_argument("--payment", choices=["wallet", "card"], help="Defaults to E2E_PAYMENT or wallet. Uses the selected method for new sandbox orders.")
    return parser


def smoke(demo):
    demo.complete_session("deployment-smoke")
    demo.data["smoke"] = {"at":now(), "reconciliation":demo.reconcile()}
    demo.journal.save()


if __name__ == "__main__":
    parser = arguments(argparse.ArgumentParser(description=__doc__))
    parser.add_argument("--require-mode", choices=["live", "hosted"], default="live", help="Demand this actual GPU mode throughout the run.")
    parser.add_argument("--repeats", type=int, default=3, choices=range(1, 4))
    parser.add_argument("--smoke", action="store_true", help="One live session, then full reconciliation.")
    args = parser.parse_args()
    try:
        execute(args, smoke if args.smoke else lambda demo: demo.acceptance(args.repeats))
    except (RuntimeError, ValueError, httpx.HTTPError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
