"use client";
import { useEffect, useState } from "react";
import {
  api,
  count,
  describe,
  explanationLabel,
  latency,
  usd,
  useResource,
} from "../lib/client";
import type {
  ApplicationEvidence,
  ContextRow,
  Quote,
  Run,
  Task,
  Turn,
} from "../lib/types";
import { useSession } from "./shell";
import { Empty, Failure, PrefixStack, Skeleton, Tag } from "./ui";
import { ArchivedPrefix } from "./archive";
import { GeneratedApplication } from "./application-check";
import { Wallet } from "./wallet";

type StreamItem = { event: string; text: string; label?: string };
function RunFeed({
  run,
  onComplete,
}: {
  run: Run;
  onComplete: () => Promise<void>;
}) {
  const [viewTurn, setViewTurn] = useState<number | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]),
    [activity, setActivity] = useState<StreamItem[]>([]),
    [error, setError] = useState(""),
    [terminal, setTerminal] = useState<string | null>(null),
    [evidence, setEvidence] = useState<ApplicationEvidence | null>(null),
    [attempt, setAttempt] = useState(0);
  useEffect(() => {
    setViewTurn(null);
    setTurns([]);
    setActivity([]);
    setTerminal(null);
    setEvidence(null);
    setError("");
    const source = new EventSource(
      `/api/agent/runs/${encodeURIComponent(run.id)}/events`,
    );
    const seen = new Set<string>();
    for (const name of [
      "quote",
      "queued",
      "decision",
      "turn",
      "capture",
      "refund",
      "error",
      "done",
    ]) {
      source.addEventListener(name, (raw: Event) => {
        if (!("data" in raw)) return;
        const msg = raw as MessageEvent<string>;
        if (msg.lastEventId && seen.has(msg.lastEventId)) return;
        if (msg.lastEventId) seen.add(msg.lastEventId);
        try {
          const d = JSON.parse(msg.data);
          if (name === "turn") {
            const t = d.call || d;
            setTurns((prev) =>
              prev.some((x) => x.id === t.id && x.turn === t.turn)
                ? prev
                : [...prev, t],
            );
          } else if (name === "error") {
            setError(d.message || d.detail || "The agent run failed.");
            setTerminal("failed");
            source.close();
            void onComplete().catch((e) => setError(describe(e)));
          } else if (name === "done") {
            setTerminal(d.status || "completed");
            if (d.application_check) setEvidence(d);
            source.close();
            void onComplete().catch((e) => setError(describe(e)));
            setActivity((p) => [
              ...p,
              { event: name, text: d.status || "Finished" },
            ]);
          } else {
            if (name === "capture" || name === "refund")
              void onComplete().catch((e) => setError(describe(e)));
            setActivity((p) => [
              ...p,
              {
                event: name,
                label:
                  name === "decision" ? explanationLabel(d.source) : undefined,
                text:
                  (name === "quote"
                    ? `Shared ${usd(d.buy_micros)} · independent ${usd(d.compute_micros)}`
                    : "") ||
                  (name === "queued"
                    ? `Queue position ${d.position} · ${d.mode}`
                    : "") ||
                  d.reason ||
                  d.explanation ||
                  d.message ||
                  d.decision ||
                  d.paypal_capture_id ||
                  d.paypal_refund_id ||
                  JSON.stringify(d),
              },
            ]);
          }
        } catch {
          setError("The server returned an unreadable event.");
          source.close();
        }
      });
    }
    source.onerror = (e) => {
      if (!("data" in e))
        setError("Event connection interrupted. Reconnecting to the run…");
    };
    source.onopen = () => setError("");
    return () => source.close();
  }, [run.id, attempt, onComplete]);
  const started =
    turns.length > 0 ||
    activity.some(
      (item) => item.event === "decision" || item.event === "quote",
    );
  const state =
    terminal || (started && run.status === "queued" ? "running" : run.status);
  const finished = state === "completed" || state === "failed";
  const finalTurn = turns.length
    ? Math.max(...turns.map((t) => t.turn))
    : undefined;
  const responseId = (n: number) => `response-${run.id}-${n}`;
  const latest = turns.find((t) => t.turn === viewTurn) || turns.at(-1);
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Run activity</h2>
          <p className="mono">{run.id}</p>
          {run.operator_seeded && (
            <Tag tone="warn">Operator-generated test session</Tag>
          )}
        </div>
        <Tag tone={state === "failed" ? "bad" : finished ? "neutral" : "good"}>
          Metered run · {state}
        </Tag>
      </div>
      <Failure error={error} retry={() => setAttempt((n) => n + 1)} />
      {(finished || evidence?.application_check || run.application_check) && (
        <GeneratedApplication
          evidence={evidence || run}
          finalTurn={terminal === "completed" ? finalTurn : undefined}
          answerLink={(n) => "#" + responseId(n)}
        />
      )}
      {activity.length > 0 && (
        <ul className="events">
          {activity.map((item, i) => (
            <li key={i}>
              <b>{item.event}</b>
              <span>
                {item.label && <strong>{item.label} · Platform-funded · </strong>}
                {item.text}
              </span>
            </li>
          ))}
        </ul>
      )}
      {turns.length === 0 ? (
        <Empty title="Waiting for engine events" />
      ) : (
        <>
          <label className="field prefix-select">
            Inspect measured prefix
            <select
              value={viewTurn ?? ""}
              onChange={(e) =>
                setViewTurn(e.target.value ? Number(e.target.value) : null)
              }
            >
              <option value="">Latest turn</option>
              {turns.map((t) => (
                <option key={t.turn} value={t.turn}>
                  Turn {t.turn}
                </option>
              ))}
            </select>
          </label>
          <PrefixStack
            source={`Workspace run · ${latest?.mode} · Turn ${latest?.turn} · ${latency(latest?.ttft_ms)} TTFT`}
            spans={latest?.spans || latest?.spans_json || []}
            cached={latest?.cached_tokens || 0}
          />
          <div className="table-scroller">
            <table>
              <thead>
                <tr>
                  <th>Turn</th>
                  <th>Mode</th>
                  <th className="numeric">Cache hits / input</th>
                  <th className="numeric">TTFT</th>
                  <th className="numeric">Charge</th>
                </tr>
              </thead>
              <tbody>
                {turns.map((t) => (
                  <tr key={t.id || t.turn}>
                    <td>
                      {t.turn}
                      {t.spans_json?.some((s) => s.kind === "publisher") &&
                        t.cached_tokens === 0 && (
                          <Tag>Cold · free public prefix</Tag>
                        )}
                      {t.sla_breach && <Tag tone="warn">SLA breach</Tag>}
                    </td>
                    <td>
                      {t.mode}
                      {t.fault_injected_ms ? (
                        <Tag tone="warn">Fault injection (test)</Tag>
                      ) : null}
                    </td>
                    <td className="numeric">
                      {count(t.cached_tokens)} / {count(t.prompt_tokens)}
                    </td>
                    <td className="numeric">{latency(t.ttft_ms)}</td>
                    <td className="numeric">{usd(t.charge_micros)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {turns.some((t) => t.output_text || t.text) && (
            <details>
              <summary>Model responses</summary>
              {turns.map((t) => (
                <section key={t.turn} id={responseId(t.turn)}>
                  <h4>Turn {t.turn}</h4>
                  <pre className="receipt">{t.output_text ?? t.text}</pre>
                </section>
              ))}
            </details>
          )}
        </>
      )}
    </section>
  );
}

export function Agent({ showWallet = true }: { showWallet?: boolean }) {
  const session = useSession(),
    catalog = useResource<ContextRow[]>("contexts", 6000),
    tasks = useResource<Task[]>("tasks");
  const [contextId, setContext] = useState(""),
    [taskId, setTask] = useState(""),
    [custom, setCustom] = useState(""),
    [fraction, setFraction] = useState("0.25");
  const [quote, setQuote] = useState<Quote | null>(null),
    [run, setRun] = useState<Run | null>(null),
    [busy, setBusy] = useState(""),
    [error, setError] = useState("");
  const health = useResource<{ mode: string }>("health", 20000);
  useEffect(() => {
    setContext(new URLSearchParams(location.search).get("context") || "");
  }, []);
  const context =
    catalog.data?.find((c) => c.id === contextId) || catalog.data?.[0];
  const availableTasks = tasks.data?.filter(
    (task) => task.source_slug === context?.slug,
  );
  const selected =
    availableTasks?.find((t) => t.id === taskId) || availableTasks?.[0];
  const actualTask = taskId === "custom" ? "custom" : selected?.id;
  const isCustom = taskId === "custom";
  const turnCount = selected?.turns || tasks.data?.[0]?.turns;
  const observed = session.state?.run;
  const currentRun =
    run && run.id !== observed?.id ? run : observed || run || null;
  const mandate = session.state?.mandate || null;
  const authorized =
    !!mandate &&
    ["AUTHORIZED", "ACTIVE"].includes(mandate.status.toUpperCase());
  useEffect(() => {
    setQuote(null);
    setError("");
  }, [context?.id, fraction, turnCount]);
  async function submit(e: React.FormEvent, launch: boolean) {
    e.preventDefault();
    if (!context || !turnCount) return;
    setBusy(launch ? "run" : "quote");
    setError("");
    try {
      const params = {
        context_id: context.id,
        est_turns: Number(turnCount),
        cold_fraction:
          health.data?.mode === "replay" && fraction !== "0"
            ? 0.25
            : Number(fraction),
      };
      if (launch && mandate) {
        const r = await api<{ run_id: string; status: string }>("agent/runs", {
          ...params,
          mandate_id: mandate.id,
          task_id: actualTask,
          task_text: isCustom ? custom : undefined,
        });
        setRun({ id: r.run_id, status: r.status });
        void session.refresh().catch((e) => setError(describe(e)));
      } else {
        setQuote(await api<Quote>("quote", params));
      }
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="stack">
      {showWallet && <Wallet mandate={mandate} onUpdate={session.refresh} />}
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Task and quote</h2>
            <p>
              Compare a warm shared prefix with the agent’s own cached context.
            </p>
          </div>
        </div>
        <Failure
          error={catalog.error || tasks.error}
          retry={() => {
            void catalog.reload();
            void tasks.reload();
          }}
        />
        {catalog.loading || tasks.loading ? (
          <Skeleton />
        ) : !context ? (
          <Empty title="Publish a context before requesting a quote" />
        ) : (
          <form
            className="form-grid"
            onSubmit={(e) =>
              void submit(
                e,
                (e.nativeEvent as SubmitEvent).submitter?.getAttribute(
                  "value",
                ) === "run",
              )
            }
          >
            <label className="field">
              Context
              <select
                value={context.id}
                onChange={(e) => setContext(e.target.value)}
              >
                {catalog.data?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title} · {c.publisher_name} ·{" "}
                    {health.data?.mode === "replay"
                      ? "Recorded workload"
                      : c.status}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              Coding task
              <select
                value={actualTask || ""}
                onChange={(e) => setTask(e.target.value)}
              >
                {availableTasks?.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.title}
                  </option>
                ))}
                {(health.data?.mode === "live" ||
                  health.data?.mode === "hosted") && (
                  <option value="custom">Custom task</option>
                )}
              </select>
            </label>
            {isCustom && (
              <label className="field full">
                Task instructions
                <textarea
                  maxLength={1500}
                  required
                  rows={3}
                  value={custom}
                  onChange={(e) => setCustom(e.target.value)}
                />
              </label>
            )}
            <label className="field">
              Task turns
              <input aria-readonly="true" readOnly value={turnCount ?? "—"} />
            </label>
            <label className="field">
              Expected cold fraction
              {health.data?.mode === "replay" ? (
                <select
                  value={fraction === "0" ? "0" : "0.25"}
                  onChange={(e) => setFraction(e.target.value)}
                >
                  <option value="0.25">0.25 · recorded buy scenario</option>
                  <option value="0">0 · recorded compute scenario</option>
                </select>
              ) : (
                <input
                  required
                  type="number"
                  min="0"
                  max="1"
                  step=".01"
                  value={fraction}
                  onChange={(e) => setFraction(e.target.value)}
                />
              )}
              <span className="field-hint">
                Share of turns expected to start with an expired private cache.
              </span>
            </label>
            <div className="actions full">
              <button className="secondary" disabled={!!busy} value="quote">
                Get quote
              </button>
              <button
                className="primary"
                disabled={
                  !authorized ||
                  !actualTask ||
                  !!busy ||
                  ["running", "queued"].includes(
                    session.state?.run?.status || "",
                  )
                }
                value="run"
              >
                {busy === "run" ? "Starting…" : "Run agent"}
              </button>
            </div>
          </form>
        )}
        <Failure error={error} />
        {quote && (
          <div className="quote-box">
            <div className="toolbar">
              <h3>Quote</h3>
              <Tag tone={quote.decision === "buy" ? "good" : "neutral"}>
                {quote.decision}
              </Tag>
            </div>
            <div className="two-col">
              <div>
                <small>Buy shared context</small>
                <strong>{usd(quote.buy_micros)}</strong>
                <small>Expected TTFT · {latency(quote.buy_ttft_ms ?? quote.warm_ttft_ms)}</small>
              </div>
              <div>
                <small>Compute independently</small>
                <strong>{usd(quote.compute_micros)}</strong>
                <small>{quote.compute_ttft_ms == null ? "Cold TTFT" : "Expected TTFT"} · {latency(quote.compute_ttft_ms ?? quote.cold_ttft_ms)}</small>
              </div>
            </div>
            {quote.reason && <p>{quote.reason}</p>}
          </div>
        )}
      </section>
      {currentRun ? (
        <RunFeed run={currentRun} onComplete={session.refresh} />
      ) : (
        <section className="panel">
          <h2>Run activity</h2>
          <Empty title="No agent run in this workspace">
            Approve a mandate and start a task to see verified hits, charges and
            model output.
          </Empty>
          <ArchivedPrefix />
        </section>
      )}
    </div>
  );
}
