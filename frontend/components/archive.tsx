"use client";
import { useState } from "react";
import { count, latency, useResource } from "../lib/client";
import type { Span } from "../lib/types";
import { Failure, PrefixStack, Skeleton, Tag } from "./ui";

type ArchiveTurn = {
  task_id: string;
  model: string;
  mode: "recorded";
  turn: number;
  cached_tokens: number;
  prompt_tokens: number;
  ttft_ms: number;
  spans: Span[];
};

export function ArchivedPrefix({ expanded = false }: { expanded?: boolean }) {
  const [open, setOpen] = useState(expanded);
  const reference = useResource<ArchiveTurn>(open ? "reference-turn" : null);
  return (
    <details
      open={open}
      onToggle={(e) => setOpen(e.currentTarget.open)}
      className="archive-prefix"
    >
      <summary>Inspect an archived GPU turn</summary>
      {open &&
        (reference.loading ? (
          <Skeleton label="Loading archived measurement" />
        ) : reference.error ? (
          <Failure error={reference.error} retry={reference.reload} />
        ) : (
          reference.data && (
            <div className="archive-turn">
              <div className="toolbar">
                <h3>Archived prefix measurement</h3>
                <Tag>Recorded GPU trace</Tag>
              </div>
              <p>
                {reference.data.model} · {reference.data.task_id} · Turn{" "}
                {reference.data.turn}
              </p>
              <PrefixStack
                source={`Archived GPU trace · ${reference.data.model} · ${reference.data.task_id}, turn ${reference.data.turn} · ${latency(reference.data.ttft_ms)} TTFT`}
                spans={reference.data.spans}
                cached={reference.data.cached_tokens}
              />
              <small>
                {count(reference.data.cached_tokens)} cached /{" "}
                {count(reference.data.prompt_tokens)} input tokens ·{" "}
                {latency(reference.data.ttft_ms)} measured TTFT
              </small>
            </div>
          )
        ))}
    </details>
  );
}
