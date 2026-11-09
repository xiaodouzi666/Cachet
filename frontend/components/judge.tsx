"use client";
import { useEffect, useState } from "react";
import { ChevronRight, Compass, ShieldCheck } from "lucide-react";
import { hasRunnableLayers } from "../lib/judge";
import { useSession } from "./shell";
import { Wallet } from "./wallet";
import { Publisher } from "./publisher";
import { Agent } from "./agent";
import { api, describe, useResource } from "../lib/client";
import type { ContextRow } from "../lib/types";
import { ActionLink, Failure, Tag } from "./ui";

const steps = [
  [
    "Open a workspace",
    "Create a private workspace for this sandbox walkthrough.",
  ],
  [
    "Publish the layers",
    "Warm Starlette, then publish FastAPI with Starlette as its parent.",
  ],
  [
    "Approve a mandate",
    "Use the sandbox wallet or configured test card to authorize a spending cap.",
  ],
  [
    "Run a coding task",
    "Choose a context and task to see the agent’s purchase decision and measured turns.",
  ],
  [
    "Exercise the controls",
    "Introduce a measured fault, capture accrued usage, audit charges and close the mandate.",
  ],
  [
    "Settle and reconcile",
    "Send eligible publisher payouts and compare recorded objects against PayPal.",
  ],
] as const;
export function Judge() {
  const session = useSession(),
    [step, setStep] = useState(0),
    [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [result, setResult] = useState<{ label: string; data: unknown } | null>(
      null,
    );
  const [confirmedFlush, setConfirmedFlush] = useState(false),
    [showBuyer, setShowBuyer] = useState(false);
  const catalog = useResource<ContextRow[]>(
    session.state ? "contexts" : null,
    5000,
  );
  const [delay, setDelay] = useState("1200"),
    [calls, setCalls] = useState("3");
  useEffect(() => {
    if (session.state) {
      const saved = sessionStorage.getItem(
        "cachet-judge-" + session.state.workspace_id,
      );
      setStep(
        session.state.kind === "judge"
          ? Math.max(0, Math.min(5, Number(saved) || 0))
          : 0,
      );
    }
  }, [session.state?.workspace_id]);
  function select(n: number) {
    setStep(n);
    setError("");
    setResult(null);
    if (session.state)
      sessionStorage.setItem(
        "cachet-judge-" + session.state.workspace_id,
        String(n),
      );
  }
  async function command(key: string, path: string, body?: unknown) {
    setError("");
    setResult(null);
    setBusy(key);
    try {
      const data = await api(path, body);
      setResult({ label: key, data });
      await session.refresh();
    } catch (e) {
      setError(describe(e));
    } finally {
      setBusy("");
    }
  }
  const replay = session.mode === "replay";
  const requiredLayers = hasRunnableLayers(catalog.data || [], session.mode);
  const isJudge = session.state?.kind === "judge";
  const mandate = session.state?.mandate;
  const hasAuth = !!mandate?.authorization_id;
  const hasRun = !!session.state?.run;
  const panel = (
    <>
      {step === 0 && (
        <div className="guide-panel">
          <ShieldCheck size={28} />
          <p>
            Your browser keeps the workspace in an HTTP-only cookie. Sandbox
            actions are scoped to this workspace.
          </p>
          <button
            type="button"
            className="primary"
            disabled={session.loading}
            onClick={async () => {
              await session.start("judge");
            }}
          >
            {isJudge
              ? "Reopen workspace"
              : session.loading
                ? "Opening…"
                : "Start guided demo"}
          </button>
          {isJudge && session.state && (
            <div className="actions">
              <Tag tone="good">Workspace open</Tag>
              <code>{session.state.workspace_id}</code>
              <button className="text-button" onClick={() => select(1)}>
                Next step <ChevronRight size={15} />
              </button>
            </div>
          )}
          <Failure
            error={session.error}
            retry={() => void session.start("judge")}
          />
        </div>
      )}
      {step === 1 && isJudge && session.state && (
        <>
          <Publisher />
          {requiredLayers && (
            <button type="button" className="primary" onClick={() => select(2)}>
              Continue to authorization
            </button>
          )}
        </>
      )}
      {step === 2 && isJudge && session.state && (
        <>
          <Wallet mandate={mandate || null} onUpdate={session.refresh} />
          <section className="panel buyer-instructions">
            <h3>Sandbox buyer</h3>
            {session.state.sandbox_buyer_email ? (
              <>
                <p className="help">
                  Use this sandbox login in the PayPal approval window.
                </p>
                <code>{session.state.sandbox_buyer_email}</code>
                {session.state.sandbox_buyer_password && (
                  <div className="actions">
                    <button
                      className="text-button"
                      onClick={() => setShowBuyer((v) => !v)}
                    >
                      {showBuyer ? "Hide password" : "Show sandbox password"}
                    </button>
                    {showBuyer && (
                      <code>{session.state.sandbox_buyer_password}</code>
                    )}
                  </div>
                )}
              </>
            ) : (
              <p className="help">
                The platform has not configured a sandbox buyer account.
              </p>
            )}
          </section>
          {hasAuth && (
            <button className="primary" onClick={() => select(3)}>
              Continue to agent task
            </button>
          )}
        </>
      )}
      {step === 3 && isJudge && session.state && (
        <>
          <Agent showWallet={false} />
          {hasRun && (
            <button className="primary" onClick={() => select(4)}>
              Open test controls
            </button>
          )}
        </>
      )}
      {step === 4 && isJudge && session.state && (
        <section className="panel">
          <h2>Workspace test controls</h2>
          <div className="form-grid">
            <label className="field">
              Injected delay (ms)
              <input
                type="number"
                min={1}
                max={5000}
                value={delay}
                onChange={(e) => setDelay(e.target.value)}
              />
            </label>
            <label className="field">
              Affected calls
              <input
                type="number"
                min={1}
                max={10}
                value={calls}
                onChange={(e) => setCalls(e.target.value)}
              />
            </label>
            <div className="full">
              <button
                disabled={
                  !!busy ||
                  Number(delay) < 1 ||
                  Number(delay) > 5000 ||
                  Number(calls) < 1 ||
                  Number(calls) > 10
                }
                className="secondary"
                onClick={() =>
                  command("Fault injection", "judge/fault", {
                    ttft_delay_ms: Number(delay),
                    calls: Number(calls),
                  })
                }
              >
                Fault injection (test)
              </button>
              <p className="help">
                Applies to the next calls in this workspace. Set before or
                during an agent run.
              </p>
            </div>
          </div>
          <hr className="section-rule" />
          <h3>Global engine cache</h3>
          {session.mode === "hosted" && (
            <p className="help">
              Global cache flush is available only on the self-hosted GPU.
            </p>
          )}
          {replay && (
            <p className="help">
              Cache flush requires the live GPU. Recorded workloads preserve the
              captured cache state.
            </p>
          )}
          <label className="check-label">
            <input
              type="checkbox"
              disabled={session.mode !== "live"}
              checked={confirmedFlush}
              onChange={(e) => setConfirmedFlush(e.target.checked)}
            />
            Clear the shared cache for all users and rewarm published contexts.
          </label>
          <button
            disabled={session.mode !== "live" || !!busy || !confirmedFlush}
            className="secondary"
            onClick={() => {
              setConfirmedFlush(false);
              void command("Cache flush", "judge/flush-cache", {});
            }}
          >
            Clear cache (test)
          </button>
          <hr className="section-rule" />
          <p className="help">
            Judge thresholds permit small sandbox charges for this
            demonstration.
          </p>
          <div className="actions">
            <button
              className="secondary"
              disabled={!!busy || !hasAuth}
              onClick={() =>
                command("Capture now", "judge/capture-now", {
                  mandate_id: mandate?.id,
                })
              }
            >
              Capture now (test)
            </button>
            <button
              className="secondary"
              disabled={!!busy || !hasAuth}
              onClick={() =>
                command("Audit", "judge/audit", { mandate_id: mandate?.id })
              }
            >
              Audit SLA (test)
            </button>
            <button
              className="secondary"
              disabled={
                !!busy ||
                !hasAuth ||
                ["CLOSED", "VOIDED"].includes(
                  mandate?.status.toUpperCase() || "",
                )
              }
              onClick={() =>
                command("Close mandate", `mandates/${mandate?.id}/close`, {})
              }
            >
              Close mandate
            </button>
          </div>
        </section>
      )}
      {step === 5 && isJudge && session.state && (
        <section className="panel">
          <h2>Publisher settlement</h2>
          <p className="help">
            Settle eligible royalties in the sandbox, then fetch PayPal
            readbacks for reconciliation.
          </p>
          <div className="actions">
            <button
              className="primary"
              disabled={!!busy}
              onClick={() => command("Settlement", "judge/settle", {})}
            >
              Settle royalties (test)
            </button>
            <button
              className="secondary"
              disabled={!!busy}
              onClick={() => command("Reconciliation", "reconcile")}
            >
              Reconcile with PayPal
            </button>
            <ActionLink quiet href="/settlement">
              Settlement desk
            </ActionLink>
          </div>
        </section>
      )}
      {busy && <p role="status">Waiting for {busy.toLowerCase()}…</p>}
      <Failure error={error} />
      {result && (
        <section className="panel">
          <h3>{result.label} result</h3>
          <pre className="receipt" data-testid="judge-result">
            {JSON.stringify(result.data, null, 2)}
          </pre>
        </section>
      )}
    </>
  );
  return (
    <div className="judge-layout">
      <aside className="panel guide-menu">
        <h2>
          <Compass size={18} /> Guided demo
        </h2>
        <ol>
          {steps.map(([title], i) => (
            <li key={title}>
              <button
                data-step={i + 1}
                aria-current={step === i ? "step" : undefined}
                disabled={!isJudge && i > 0}
                onClick={() => select(i)}
              >
                <span>{String(i + 1).padStart(2, "0")}</span>
                {title}
              </button>
            </li>
          ))}
        </ol>
        {isJudge && (
          <button
            className="text-button"
            onClick={async () => {
              await session.start("user", true);
              select(0);
            }}
          >
            Exit judge mode
          </button>
        )}
      </aside>
      <div className="guide-content stack">
        <div>
          <small className="eyebrow">Step {step + 1} of 6</small>
          <h2>{steps[step][0]}</h2>
          <p>
            {step === 1 && replay
              ? "Register Starlette and its FastAPI child to run the recorded workload."
              : steps[step][1]}
          </p>
        </div>
        {panel}
      </div>
    </div>
  );
}
