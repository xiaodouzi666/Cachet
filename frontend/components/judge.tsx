"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ArrowRight, Check, ChevronRight, RefreshCw } from "lucide-react";
import { api, describe, latency, usd, useResource } from "../lib/client";
import type { Mandate, Quote, Run } from "../lib/types";
import { RunFeed } from "./agent";
import { useSession } from "./shell";
import { Failure, FeeHint, MoneyStrip, Tag } from "./ui";
import { Wallet } from "./wallet";

const titles = [
  "Workspace",
  "Mandate",
  "Agent task",
  "Capture now",
  "Fault and refund",
  "Close and settle",
];
const descriptions = [
  "Open a private workspace and verify both published sources are warm.",
  "Authorize a $5 sandbox budget with a PayPal wallet or the test card.",
  "Choose a coding task, compare prices and follow its metered turns.",
  "Collect the accrued whole cents and inspect PayPal’s actual fee.",
  "Add three delayed calls on this mandate, capture their billable portions and audit the measured breaches.",
  "Release the remaining authorization, pay both maintainers and reconcile the receipts with PayPal.",
];
type Preparation = {
  status: string;
  turns?: number;
  completed_runs?: number;
  captured_micros?: number;
  forgiven_micros?: number;
  fee_micros?: number;
  error?: string;
  publisher_funding?: {
    slug: string;
    available_micros: number;
    threshold_micros: number;
  }[];
};
type Guide = {
  steps: boolean[];
  contexts: {
    id: string;
    slug: string;
    status: string;
    probe: number | null;
  }[];
  mandate?: Mandate;
  run?: Run;
  followup?: Run;
  preparation: Preparation;
  captures?: {
    id: string;
    amount_cents: number;
    fee_cents: number;
    net_cents: number;
  }[];
  refunds?: { id: string; amount_cents: number; call_ids: string[] }[];
  fault_calls?: string[];
  settlement?: {
    paypal_batch_id?: string;
    status?: string;
    items?: {
      id: string;
      amount_cents: number;
      fee_cents: number;
      status: string;
    }[];
    reconciliation?: { ok: boolean; checked: number; clearing_micros: number };
  };
};
type Preview = {
  amount_cents: number;
  estimated_fee_cents: number;
  estimated_net_cents: number;
  below_threshold: boolean;
};
const cents = (v: number) => usd(v * 10_000, 2);
const delay = (ms: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, ms));

function PreparationStatus({
  row,
  settled = false,
}: {
  row: Preparation;
  settled?: boolean;
}) {
  if (row.status === "not_started") return null;
  return (
    <section className="panel" aria-label="Sponsor preparation">
      <div className="panel-head">
        <h3>Publisher payout preparation</h3>
        <Tag tone={row.status === "ready" ? "good" : "neutral"}>
          {row.status}
        </Tag>
      </div>
      <p className="help">
        Separate operator-funded sandbox tasks in this demo earn the
        maintainers’ minimum payouts. Your mandate pays for your own runs.
      </p>
      <dl className="guide-facts">
        <div>
          <dt>Sponsor turns</dt>
          <dd>{row.turns ?? 0}</dd>
        </div>
        <div>
          <dt>Captured</dt>
          <dd>{usd(row.captured_micros ?? 0)}</dd>
        </div>
        <div>
          <dt>PayPal fees</dt>
          <dd>{usd(row.fee_micros ?? 0)}</dd>
        </div>
        <div>
          <dt>Forgiven</dt>
          <dd>{usd(row.forgiven_micros ?? 0)}</dd>
        </div>
      </dl>
      {row.publisher_funding?.map((p) => (
        <p className="help" key={p.slug}>
          {p.slug} · {usd(p.available_micros)}{" "}
          {settled
            ? "carried forward after payout"
            : "available · " + usd(p.threshold_micros, 2) + " minimum"}
        </p>
      ))}
      {row.error && (
        <p className="help" role="status">
          {row.error}
        </p>
      )}
    </section>
  );
}

export function Judge() {
  const session = useSession();
  const isJudge = session.state?.kind === "judge";
  const guide = useResource<Guide>(isJudge ? "judge/walkthrough" : null, 3500);
  const data = guide.data;
  const m = data?.mandate ?? session.state?.mandate ?? null;
  const [step, setStep] = useState(0);
  const [task, setTask] = useState("rate-limit");
  const [quote, setQuote] = useState<Quote | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmFlush, setConfirmFlush] = useState(false);
  const ready = useRef(false);
  const mounted = useRef(true);
  const displayStep = (n: number) => {
    setStep(n);
    setNotice("");
    setError("");
    if (session.state?.workspace_id)
      localStorage.setItem(
        "cachet-guide-v3-" + session.state.workspace_id,
        String(n),
      );
  };
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    ready.current = false;
  }, [session.state?.workspace_id]);
  useEffect(() => {
    if (!data || !session.state || ready.current) return;
    ready.current = true;
    const stored = localStorage.getItem(
      "cachet-guide-v3-" + session.state.workspace_id,
    );
    const first = data.steps.indexOf(false);
    const initial = stored === null ? (first < 0 ? 5 : first) : Number(stored);
    setStep(
      Number.isInteger(initial) && initial >= 0 && initial < 6 ? initial : 0,
    );
  }, [data, session.state]);
  const refresh = useCallback(async () => {
    await Promise.all([guide.reload(), session.refresh()]);
  }, [guide.reload, session.refresh]);
  const preview = useResource<Preview>(
    step === 3 && isJudge && m
      ? "judge/capture-preview?mandate_id=" + encodeURIComponent(m.id)
      : null,
  );
  const ctx = data?.contexts.find(
    (c) => c.slug === (task === "rate-limit" ? "fastapi" : "starlette"),
  );
  useEffect(() => {
    if (!isJudge || !ctx || step !== 2) {
      setQuote(null);
      return;
    }
    const controller = new AbortController();
    void api<Quote>(
      "quote",
      { context_id: ctx.id, cold_fraction: 0.25, est_turns: 10 },
      controller.signal,
    )
      .then(setQuote)
      .catch((e) => {
        if (!controller.signal.aborted) setError(describe(e));
      });
    return () => controller.abort();
  }, [ctx?.id, isJudge, step]);
  async function act(label: string, fn: () => Promise<void>) {
    if (busy) return;
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await fn();
      if (mounted.current) await refresh();
    } catch (e) {
      if (mounted.current) setError(describe(e));
    } finally {
      if (mounted.current) setBusy("");
    }
  }
  async function start() {
    await session.start("judge");
    await api("judge/prepare", {});
    await guide.reload();
  }
  async function fault() {
    if (!m || !data?.run) return;
    const test = await api<{ run_id: string }>("judge/fault-run", {
      mandate_id: m.id,
      run_id: data.run.id,
    });
    const body = { mandate_id: m.id, run_id: test.run_id };
    const end = Date.now() + 10 * 60 * 1000;
    while (mounted.current && Date.now() < end) {
      const current = await api<Guide>("judge/walkthrough");
      if (current.followup?.status === "failed")
        throw new Error("The test run failed. Inspect its run activity.");
      if (current.followup?.status === "completed") {
        await api("judge/fault-capture", body);
        await api("judge/fault-audit", body);
        return;
      }
      setNotice(
        "The three test calls are running. Each call incurs a real delay.",
      );
      await delay(1800);
    }
    if (mounted.current)
      throw new Error(
        "The test is still running. Resume this step to check its original run.",
      );
  }
  async function finish() {
    if (!m) return;
    const end = Date.now() + 12 * 60 * 1000;
    while (mounted.current && Date.now() < end) {
      const next = await api<Guide>("judge/finish", { mandate_id: m.id });
      await guide.reload();
      if (next.steps[5]) return;
      if (["failed", "exhausted"].includes(next.preparation.status))
        throw new Error(
          "Sponsor preparation ended before both recipients reached the payout minimum.",
        );
      setNotice(
        next.preparation.status === "ready"
          ? "Waiting for PayPal to confirm both payout items."
          : "Your mandate is closed. Waiting for the sponsor’s separate funded publisher balances.",
      );
      await delay(6000);
    }
    if (mounted.current)
      throw new Error(
        "Settlement is still pending. Resume this step to check the original batch.",
      );
  }
  useEffect(() => {
    if (data?.run?.task_id) setTask(data.run.task_id);
  }, [data?.run?.task_id]);
  const flags = data?.steps ?? Array<boolean>(6).fill(false);
  const firstOpen = flags.indexOf(false);
  const navNext =
    flags[step] && step < 5 ? (
      <button className="text-button" onClick={() => displayStep(step + 1)}>
        Continue to {titles[step + 1]} <ChevronRight size={16} />
      </button>
    ) : null;
  return (
    <div className="judge-layout" data-testid="judge-guide">
      <aside className="guide-menu" aria-label="Walkthrough steps">
        <ol>
          {titles.map((name, i) => (
            <li key={name}>
              <button
                type="button"
                aria-current={step === i ? "step" : undefined}
                disabled={firstOpen >= 0 && i > firstOpen}
                aria-label={name + (flags[i] ? " · complete" : "")}
                data-testid={"guide-step-" + (i + 1)}
                data-complete={flags[i]}
                onClick={() => displayStep(i)}
              >
                <span>
                  {flags[i] ? <Check size={15} aria-hidden /> : i + 1}
                </span>
                {name}
              </button>
            </li>
          ))}
        </ol>
        {isJudge && <button className="text-button" onClick={async () => {
          await session.start("user", true); displayStep(0);
        }}>Exit judge mode</button>}
      </aside>
      <div className="guide-content stack">
        <div>
          <small className="eyebrow">Step {step + 1} of 6</small>
          <h2>{titles[step]}</h2>
          <p>{descriptions[step]}</p>
        </div>
        <Failure
          error={error || guide.error || session.error}
          retry={() => void refresh()}
        />
        {(busy || notice) && (
          <p role="status" className="help">
            {notice || busy + "…"}
          </p>
        )}
        {step === 0 && session.mode === "replay" && (
          <section className="panel">
            <h3>Explore the recorded GPU runs</h3>
            <p>The live GPU is offline. Inspect ten measured turns from each built-in task while the warm-cache walkthrough waits for live inference.</p>
            <Link className="text-button" href="/dev/ui">Inspect recorded runs <ArrowRight size={15} /></Link>
          </section>
        )}
        {step === 0 && (
          <section className="panel">
            <button
              className="primary"
              disabled={!!busy}
              onClick={() => void act("Preparing workspace", start)}
            >
              {data?.preparation.status &&
              data.preparation.status !== "not_started"
                ? "Reopen workspace"
                : "Start guided demo"}{" "}
              <ArrowRight size={16} />
            </button>
            {!data?.preparation || data.preparation.status === "not_started" ? <p className="help">
            Starting prepares two maintainer payouts with a separate operator-funded sandbox authorization of up to $1.
          </p> : null}
          {data?.contexts.map((c) => (
              <div className="guide-source" key={c.id}>
                <strong>{c.slug}</strong>
                <Tag tone={c.status === "hot" ? "good" : "neutral"}>
                  {c.status}
                </Tag>
                <span className="mono">
                  {c.probe == null
                    ? "Probe pending"
                    : (c.probe * 100).toFixed(3) + "% hit"}
                </span>
                <button
                  className="text-button"
                  disabled={!!busy}
                  onClick={() =>
                    void act("Warming source", async () => {
                      await api("contexts/" + c.id + "/warm", {});
                    })
                  }
                >
                  <RefreshCw size={14} /> Re-warm
                </button>
              </div>
            ))}
            {navNext}
          </section>
        )}
        {step === 0 && data?.preparation && (
          <PreparationStatus row={data.preparation} settled={flags[5]} />
        )}
        {step === 1 && isJudge && (
          <>
            <Wallet mandate={m} onUpdate={refresh} guided />
            {session.state?.sandbox_buyer_email && (
              <details className="panel">
                <summary>Sandbox wallet login</summary>
                <p>{session.state.sandbox_buyer_email}</p>
                <code>{session.state.sandbox_buyer_password}</code>
              </details>
            )}
            {navNext}
          </>
        )}
        {step === 2 && isJudge && (
          <>
            <section className="panel">
              <label className="field">
                Built-in task
                <select
                  value={task}
                  disabled={!!data?.run}
                  onChange={(e) => setTask(e.target.value)}
                >
                  <option value="rate-limit">Add FastAPI rate limiting</option>
                  <option value="route-class">
                    Add a custom Starlette route
                  </option>
                </select>
              </label>
              {quote && (
                <div className="quote-pair">
                  <div>
                    <small>Shared cache</small>
                    <strong>{usd(quote.buy_micros)}</strong>
                    <small>
                      Expected TTFT ·{" "}
                      {latency(quote.buy_ttft_ms ?? quote.warm_ttft_ms)}
                    </small>
                  </div>
                  <div>
                    <small>Private compute</small>
                    <strong>{usd(quote.compute_micros)}</strong>
                    <small>
                      Expected TTFT ·{" "}
                      {latency(quote.compute_ttft_ms ?? quote.cold_ttft_ms)}
                    </small>
                  </div>
                </div>
              )}
              <button
                className="primary"
                disabled={
                  !!busy || !m?.authorization_id || !!data?.run || !quote
                }
                onClick={() =>
                  void act("Starting coding task", async () => {
                    await api("judge/task", {
                      mandate_id: m!.id,
                      task_id: task,
                    });
                  })
                }
              >
                Run agent task
              </button>
              {navNext}
            </section>
            {data?.run && (
              <RunFeed key={data.run.id} run={data.run} onComplete={refresh} />
            )}
            {m && <MoneyStrip mandate={m} />}
          </>
        )}
        {step === 3 && isJudge && (
          <section className="panel">
            <FeeHint amountCents={preview.data?.amount_cents} feeCents={preview.data?.estimated_fee_cents} />
            <button
              className="primary"
              disabled={!!busy || !flags[2] || flags[3]}
              onClick={() =>
                void act("Capturing accrued cents", async () => {
                  await api("judge/capture-now", { mandate_id: m!.id });
                  await preview.reload();
                })
              }
            >
              Capture now (test)
            </button>
            {(data?.captures || []).map((c) => (
              <p className="help" key={c.id}>
                <code>{c.id}</code> · captured {cents(c.amount_cents)} · actual
                fee {cents(c.fee_cents)} · net {cents(c.net_cents)}
              </p>
            ))}
            {preview.data?.below_threshold && (
              <p className="help">
                Below the $2.00 batch threshold — in production this charge
                would wait for the batch or be forgiven at close.
              </p>
            )}
            {navNext}
          </section>
        )}
        {step === 4 && isJudge && (
          <>
            <section className="panel">
              <button
                className="primary"
                disabled={!!busy || !flags[3] || flags[4]}
                onClick={() =>
                  void act("Testing and auditing three calls", fault)
                }
              >
                {data?.followup
                  ? "Resume fault test"
                  : "Inject three delays, capture and audit (test)"}
              </button>
              {data?.refunds?.map((r) => (
                <p key={r.id}>
                  <code>{r.id}</code> · PayPal refunded {cents(r.amount_cents)}{" "}
                  across {r.call_ids.length} linked calls.
                </p>
              ))}
              {navNext}
            </section>
            {data?.followup && (
              <RunFeed
                key={data.followup.id}
                run={data.followup}
                onComplete={refresh}
              />
            )}
          </>
        )}
        {step === 5 && isJudge && (
          <>
            <section className="panel">
              <MoneyStrip mandate={m} />
              <button
                className="primary"
                disabled={!!busy || !flags[4] || flags[5]}
                onClick={() => void act("Closing and settling", finish)}
              >
                Close, settle and reconcile
              </button>
              {data?.settlement?.paypal_batch_id && (
                <p>
                  Payout batch <code>{data.settlement.paypal_batch_id}</code> ·{" "}
                  {data.settlement.status}
                </p>
              )}
              {data?.settlement?.items?.map((i) => (
                <p key={i.id}>
                  <code>{i.id}</code> · {cents(i.amount_cents)} · fee{" "}
                  {cents(i.fee_cents)} · {i.status}
                </p>
              ))}
              {data?.settlement?.reconciliation?.ok && (
                <p role="status">
                  <strong>Reconciled</strong> ·{" "}
                  {data.settlement.reconciliation.checked} PayPal objects · zero
                  differences.
                </p>
              )}
            </section>
            {data?.preparation && (
              <PreparationStatus row={data.preparation} settled={flags[5]} />
            )}
          </>
        )}
        {isJudge && (
          <details className="panel">
            <summary>Additional cache test</summary>
            <p>
              Clear the global live prefix cache to observe a cold, free public
              prefix. Other live workspaces share this cache.
            </p>
            <button
              className="secondary"
              disabled={!!busy || session.mode !== "live"}
              onClick={() => {
                if (!confirmFlush) {
                  setConfirmFlush(true);
                  return;
                }
                setConfirmFlush(false);
                void act("Clearing live cache", async () => {
                  await api("judge/flush-cache", {});
                  setNotice("Global cache flush confirmed.");
                });
              }}
            >
              {confirmFlush
                ? "Confirm global flush"
                : "Flush cache (global, rate-limited)"}
            </button>
          </details>
        )}
      </div>
    </div>
  );
}
