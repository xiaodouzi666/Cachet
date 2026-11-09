"use client";
import { useState } from "react";
import {
  count,
  explanationLabel,
  latency,
  usd,
  useResource,
} from "../lib/client";
import type {
  ApplicationEvidence,
  DecisionExplanation,
  Quote,
  Span,
  Task,
} from "../lib/types";
import { GeneratedApplication } from "./application-check";
import { Failure, PrefixStack, Skeleton, Tag } from "./ui";

type Strategy = "buy" | "compute";
type ArchiveTurn = ApplicationEvidence & {
  decision?: Strategy;
  decision_explanation?: DecisionExplanation | null;
  quote?: Pick<Quote, "decision" | "buy_micros" | "compute_micros">;
  final_turn?: number;
  final_output_text?: string;
  earlier_verified_output_text?: string;
  task_id: string;
  model: string;
  recorded_at?: string;
  mode: "recorded";
  turn: number;
  cached_tokens: number;
  prompt_tokens: number;
  ttft_ms: number;
  spans: Span[];
};
const strategyLabels = {
  buy: "Buy shared context",
  compute: "Compute privately",
};
function referencePath(task: string, strategy: Strategy) {
  return `reference-turn?task_id=${encodeURIComponent(task)}&decision=${strategy}`;
}
function RecordingTime({ at }: { at: string }) {
  return (
    <time dateTime={at}>
      {new Date(at).toLocaleString("en-GB", {
        dateStyle: "medium",
        timeStyle: "medium",
        timeZone: "UTC",
      })}{" "}
      UTC
    </time>
  );
}

function Measurement({
  track,
  alternate = false,
}: {
  track: ArchiveTurn;
  alternate?: boolean;
}) {
  return (
    <div
      className="archive-turn"
      data-task={track.task_id}
      data-decision={track.decision}
      data-alternate={alternate}
    >
      <div className="toolbar">
        <h3>
          {alternate
            ? "Independent strategy recording"
            : "Archived prefix measurement"}
        </h3>
        <Tag>Recorded GPU trace</Tag>
      </div>
      <p>
        {track.model} · {track.task_id}
        {track.decision && ` · ${strategyLabels[track.decision]}`} · Turn{" "}
        {track.turn}
        {track.recorded_at && (
          <>
            {" "}
            · Coding run recorded <RecordingTime at={track.recorded_at} />
          </>
        )}
      </p>
      <PrefixStack
        spans={track.spans}
        cached={track.cached_tokens}
        source={`Archived GPU trace · ${track.model} · ${track.task_id}, turn ${track.turn} · ${latency(track.ttft_ms)} TTFT`}
      />
      <small>
        {count(track.cached_tokens)} cached / {count(track.prompt_tokens)} input
        tokens · {latency(track.ttft_ms)} measured TTFT
      </small>
      {track.quote && (
        <section className="quote-box" aria-label="Archived strategy decision">
          <div className="toolbar">
            <h4>
              {explanationLabel(track.decision_explanation?.source) ||
                "Recorded quote"}
            </h4>
            <Tag>{track.quote.decision}</Tag>
          </div>
          <div className="two-col">
            <div>
              <small>Buy shared context</small>
              <strong>{usd(track.quote.buy_micros)}</strong>
            </div>
            <div>
              <small>Compute privately</small>
              <strong>{usd(track.quote.compute_micros)}</strong>
            </div>
          </div>
          {track.decision_explanation?.reason && (
            <p>{track.decision_explanation.reason}</p>
          )}
          {track.decision_explanation?.source === "recorded" && (
            <small>
              Platform-funded · Generated separately for this quote
              {track.decision_explanation.generated_at && (
                <>
                  {" "}
                  ·{" "}
                  <RecordingTime at={track.decision_explanation.generated_at} />
                </>
              )}
            </small>
          )}
        </section>
      )}
      <GeneratedApplication
        evidence={track}
        finalTurn={track.final_turn}
        finalResponse={track.final_output_text}
        earlierResponse={track.earlier_verified_output_text}
      />
    </div>
  );
}

export function ArchivedPrefix({ expanded = false }: { expanded?: boolean }) {
  const [open, setOpen] = useState(expanded);
  const [task, setTask] = useState("rate-limit"),
    [decision, setDecision] = useState<Strategy>("buy");
  const tasks = useResource<Task[]>(open ? "tasks" : null);
  const reference = useResource<ArchiveTurn>(
    open ? referencePath(task, decision) : null,
  );
  const track = reference.data;
  const matches =
    track?.task_id === task &&
    (track.decision === decision ||
      (!track.decision && task === "rate-limit" && decision === "buy"));
  const other = decision === "buy" ? "compute" : "buy";
  const candidate = useResource<ArchiveTurn>(
    open && matches && track?.application_check?.status === "failed"
      ? referencePath(task, other)
      : null,
  ).data;
  const alternate =
    candidate?.task_id === task &&
    candidate.decision === other &&
    candidate.application_check?.status === "passed"
      ? candidate
      : null;
  return (
    <details
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="archive-prefix"
    >
      <summary>Inspect an archived GPU turn</summary>
      {open && (
        <>
          <div className="form-grid archive-controls">
            <label className="field">
              Recorded coding task
              <select value={task} onChange={(e) => setTask(e.target.value)}>
                {(tasks.data || []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.title}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              Recorded strategy
              <select
                value={decision}
                onChange={(e) => setDecision(e.target.value as Strategy)}
              >
                <option value="buy">{strategyLabels.buy}</option>
                <option value="compute">{strategyLabels.compute}</option>
              </select>
            </label>
            <Failure error={tasks.error} retry={tasks.reload} />
          </div>
          {reference.loading ? (
            <Skeleton label="Loading archived measurement" />
          ) : reference.error ? (
            <Failure error={reference.error} retry={reference.reload} />
          ) : track && matches ? (
            <Measurement track={track} />
          ) : (
            <Failure
              error="The requested archived track was not returned."
              retry={reference.reload}
            />
          )}
          {alternate && (
            <details key={task + other} className="alternate-archive">
              <summary>View passing {other} archive for this task</summary>
              <Measurement track={alternate} alternate />
            </details>
          )}
        </>
      )}
    </details>
  );
}
