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

from scripts.gpu_lease import gpu_lease

SOURCES = ("starlette", "fastapi")
# Preparation earns real publisher balances with short, read-only framework tasks.
# The acceptance sessions always use the built-in coding task and its full contract.
PREPARATION_TASKS = (
    "Trace dependency resolution in FastAPI and show a minimal request-header dependency.",
    "Explain how a FastAPI route dispatches into Starlette and show a small ASGI example.",
    "Trace FastAPI exception handling through Starlette and demonstrate a custom exception handler.",
)
SETTLED = {"CLOSED", "VOIDED", "EXPIRED"}
DEFAULT_STATE = ROOT / "logs" / "demo-workspace-v3.json"


def origin(url):
    parsed = urlsplit(url)
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))):
        raise ValueError("Use an HTTPS backend origin, or a loopback HTTP origin for local development.")
    return url.rstrip("/")


def admin_for_origin(config, url):
    if urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1"):
        return require(config, "ADMIN_TOKEN")
    return config.get("PRODUCTION_ADMIN_TOKEN") or require(config, "ADMIN_TOKEN")


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

    def connect(self, api_url, *, workspace_kind="judge"):
        check(workspace_kind in ("judge", "user"), "Choose a known workspace kind.")
        check(self.data.setdefault("origin", api_url) == api_url, "Resume this workspace on its original backend.")
        if self.data.get("workspace_cookie"):
            self.api.cookies.set("cachet_workspace", self.data["workspace_cookie"])
            state = self.call("GET", "/judge/state")
            check(self.data["workspace_id"] == state["workspace_id"] and state["kind"] == workspace_kind,
                  "The journal's original workspace is required.")
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
            state = self.call("POST", "/judge/workspace", {"kind":workspace_kind})
            self.data.update(workspace_id=state["workspace_id"], workspace_cookie=self.api.cookies["cachet_workspace"])
            self.journal.save()
        check(state["kind"] == workspace_kind, "The operator journal workspace kind differs.")
        self.data["health"] = {key: health.get(key) for key in ("service", "version", "mode", "gpu", "model", "build")}
        with gpu_lease():
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

    def warm_for_acceptance(self):
        ctx = self.contexts["fastapi"]
        before = self.call("GET", "/contexts/" + ctx)
        self.call("POST", "/contexts/" + ctx + "/warm")
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            observed = self.call("GET", "/contexts/" + ctx)
            if (observed["status"] == "hot" and (observed.get("last_probe_hit_ratio") or 0) >= .95
                    and observed.get("last_probe_at") and observed["last_probe_at"] != before.get("last_probe_at")):
                return {k:observed[k] for k in ("id", "token_len", "last_probe_hit_ratio", "last_probe_at")}
            time.sleep(1)
        raise RuntimeError("Admission requires a freshly confirmed warm probe for the same context.")


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

    def turns(self, run_id, *, require_faults=False):
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
        # A stream can end before its terminal event. Retry that read against the same run.
        check(terminal, f"No completed event for {run_id}. Resume with the same journal.")
        if mode != self.mode or decision != "buy":
            raise FailedRun(f"Completed run {run_id} did not use the required {self.mode} Buy mode.", rows)
        if not (len(rows) >= 10 and len({r["turn"] for r in rows}) == len(rows) == len({r["id"] for r in rows})
                and all(r["mode"] == self.mode and r["prompt_tokens"] > 0 and r["output_tokens"] > 0 for r in rows)):
            raise FailedRun("Expected at least ten distinct live metered turns.", rows)
        if not all(any(r["royalty_json"].get(p, 0) > 0 for r in rows) for p in self.publishers.values()):
            raise FailedRun("Real metering must attribute positive royalties to each published layer.", rows)
        if require_faults:
            check_injected_faults(rows)
        return rows

    def session_turns(self, s):
        try:
            return self.turns(s["run_id"], require_faults=s["purpose"] == "acceptance")
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
        with gpu_lease():
            if not s.get("run_id"):
                # A previous process may have accepted the POST and lost its response.
                last = self.call("GET", "/judge/state").get("run")
                if last and last["mandate_id"] == mid:
                    s.update(run_id=last["id"], accepted_at=time.time())
                else:
                    check(mandate["status"] == "ACTIVE", "This mandate closed before a run started. Inspect its sandbox authorization.")
                    if purpose == "acceptance":
                        s["admission_warm"] = self.warm_for_acceptance()
                        self.journal.save()
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
                    except RateLimit:
                        # The same active mandate remains available across task admission windows.
                        self.journal.save()
                        raise
                    s.update(run_id=run["run_id"], accepted_at=time.time())
                self.journal.save()
            rows = self.session_turns(s)
        faults = [r for r in rows if r["fault_injected_ms"]]
        s["turns"] = rows
        self.journal.save()
        if mandate["status"] not in SETTLED and purpose != "preparation":
            preview = self.call("GET", "/judge/capture-preview?mandate_id=" + mid)
            check("estimated_fee_cents" in preview and "estimated_net_cents" in preview, "Fee-aware preview is required.")
            if preview["amount_cents"] > 0:
                receipt = self.call("POST", "/judge/capture-now", {"mandate_id":mid})
                check(receipt["amount_cents"] == preview["amount_cents"] and
                      receipt["net_cents"] == receipt["amount_cents"] - receipt["fee_cents"], "Capture gross, fee and net differ.")
                s["capture_now"] = {"preview":preview, "receipt":receipt}
                self.journal.save()
        self.call("POST", "/judge/audit", {"mandate_id":mid})
        before_close = self.call("GET", f"/mandates/{mid}")
        closed = self.call("POST", f"/mandates/{mid}/close")
        check(closed["status"] in {"CLOSED", "VOIDED"} and
              0 <= closed["refunded_micros"] <= closed["captured_micros"] <= closed["cap_micros"],
              "Closed mandate totals are inconsistent.")
        if before_close["status"] not in SETTLED:
            remainder = before_close["accrued_uncaptured_micros"]
            if 0 < remainder < closed["final_capture_min_micros"]:
                check(closed["forgiven_micros"] == remainder and closed["captured_micros"] == before_close["captured_micros"],
                      "Subminimum final accrual was not forgiven.")
        check(closed["released_micros"] == closed["cap_micros"] - closed["captured_micros"], "Released budget differs.")
        captures = [c for c in self.call("GET", "/captures") if c["mandate_id"] == mid]
        receipts = [r for r in self.call("GET", "/refunds") if r["capture_id"] in {c["id"] for c in captures}]
        if purpose == "acceptance":
            check(captures and all(c["paypal_capture_id"] and c["net_cents"] == c["amount_cents"] - c["fee_cents"] for c in captures),
                  "Missing real capture gross, fee or net.")
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

    def acceptance(self, repeats, *, new_suite=False):
        # Start with enough pre-existing real earnings that refunds cannot strand one recipient below a cent.
        accepted = lambda: sum(1 for s in self.sessions if s.get("accepted"))
        suite = self.data.get("acceptance_suite")
        if suite and suite.get("completed") and not new_suite:
            return
        if not suite or suite.get("completed"):
            suite = {"started":now(), "initial":accepted(), "repeats":repeats, "mode":self.mode, "settlement_policy":"fee-aware-v3"}
            self.data["acceptance_suite"] = suite
            self.journal.save()
        check(repeats == suite["repeats"], "Resume the suite using its original repeat count.")
        check(suite.get("mode", self.mode) == self.mode, "Resume the suite using its original inference mode.")
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


def check_injected_faults(rows):
    faults = [r for r in rows if r["fault_injected_ms"]]
    if len(faults) != 3 or not all(r["sla_breach"] and r["ttft_ms"] >= 1200 for r in faults):
        raise FailedRun("Expected three observed delays that breached the SLA.", rows)


class RateLimit(RuntimeError):
    def __init__(self, delay, route):
        self.delay, self.route = delay, route
        super().__init__(f"Rate limited on {route}. Retry after {delay}s.")


def exported_suite(path, data):
    try:
        report = json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        return False
    return (isinstance(report, dict) and report.get("workspace_id") == data["workspace_id"]
            and report.get("acceptance_suite") == data["acceptance_suite"])


def execute(args, action=None):
    os.umask(0o077)
    with locked_journal(args.state, "operator-demo") as state:
        report = args.state.with_name(args.state.stem + "-evidence.json")
        suite = state.data.get("acceptance_suite") or {}
        completed = action is None and suite.get("completed")
        start_new = completed and args.new_suite and exported_suite(report, state.data)
        if completed and not start_new:
            # A lost final export (even when resuming --new-suite) needs only this journal.
            print("Writing evidence for the completed suite from its private journal.")
        else:
            config = settings()
            payment = args.payment or config.get("E2E_PAYMENT") or "wallet"
            check(payment in {"wallet", "card"}, "E2E_PAYMENT must be wallet or card.")
            url = origin(args.api or config.get("PUBLIC_API_URL") or "http://127.0.0.1:8000")
            token = admin_for_origin(config, url)
            with PayPalClient(config) as pp, httpx.Client(base_url=url, headers={"X-Admin-Token":token},
                        follow_redirects=False, timeout=httpx.Timeout(240, connect=10)) as api:
                demo = Demo(state, api, pp, config, payment, getattr(args, "require_mode", "live"))
                if getattr(args, "catalog_only", False):
                    demo.connect(url, workspace_kind="user")
                else:
                    demo.connect(url)
                if action is None:
                    demo.acceptance(args.repeats, new_suite=bool(start_new))
                else:
                    action(demo)
        summary = {k:state.data[k] for k in ("workspace_id", "health", "warm", "sessions")}
        summary.update(traffic="Operator-run sandbox test traffic", generated_at=now())
        if state.data.get("acceptance_suite"):
            summary["acceptance_suite"] = state.data["acceptance_suite"]
            accepted = [s for s in state.data["sessions"] if s.get("accepted")]
            suite = state.data["acceptance_suite"]
            current = accepted[suite["initial"]:suite["initial"] + suite["repeats"]]
            summary["settlement_policy"] = "fee-aware-v3" if len(current) == suite["repeats"] and all(
                s.get("capture_now", {}).get("receipt", {}).get("net_cents") is not None and
                "forgiven_micros" in s.get("closed", {}) for s in current) else "prior-or-incomplete"
        if state.data.get("seed"):
            summary["seed"] = state.data["seed"]
        if state.data.get("smoke"):
            summary["smoke"] = state.data["smoke"]
        save_report(report, summary)
        print(f"Real test evidence: {report}")
        if completed and not start_new:
            check(suite["repeats"] == args.repeats and suite.get("mode", state.data["health"]["mode"]) == args.require_mode,
                  "The exported suite used a different repeat count or inference mode. Select --new-suite for a new acceptance.")


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
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--new-suite", action="store_true",
                           help="Start another paid acceptance suite after exporting the previous result.")
    operation.add_argument("--smoke", action="store_true", help="One live session, then full reconciliation.")
    args = parser.parse_args()
    try:
        execute(args, smoke if args.smoke else None)
    except (RuntimeError, ValueError, httpx.HTTPError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")
