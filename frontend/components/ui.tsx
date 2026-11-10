"use client";
import { useEffect, useRef } from "react";
import { AlertCircle, ArrowRight, CircleDashed, RefreshCw } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { count, usd } from "../lib/client";
import type { Health, Mandate, Span } from "../lib/types";

export function PageTitle({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <div>
        <h1>{title}</h1>
        {children && <p>{children}</p>}
      </div>
      {action}
    </header>
  );
}
export function ActionLink({
  href,
  children,
  quiet = false,
}: {
  href: string;
  children: ReactNode;
  quiet?: boolean;
}) {
  return (
    <Link className={quiet ? "button secondary" : "button"} href={href}>
      {children}
      <ArrowRight size={16} />
    </Link>
  );
}
export function Failure({
  error,
  retry,
}: {
  error: string;
  retry?: () => void;
}) {
  if (!error) return null;
  return (
    <div className="alert" role="alert">
      <AlertCircle size={17} />
      <span>{error}</span>
      {retry && (
        <button type="button" className="text-button" onClick={retry}>
          <RefreshCw size={14} />
          Retry
        </button>
      )}
    </div>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <CircleDashed size={22} />
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Skeleton({ label = "Loading" }: { label?: string }) {
  return (
    <div className="skeleton" role="status" aria-label={label}>
      <i />
      <i />
      <i />
    </div>
  );
}
export function Tag({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
}) {
  return <span className={`tag ${tone}`}>{children}</span>;
}
export function PrefixStack({
  spans,
  cached,
  source,
}: {
  spans: Span[];
  cached: number;
  source?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const total = spans.reduce((max, part) => Math.max(max, part.end), 0);
  if (!total) return <Empty title="No measured prefix spans yet" />;
  const labels: Record<string, string> = {
    system: "System prefix",
    starlette: "Starlette",
    fastapi: "FastAPI",
    salt: "Tenant salt",
    turns: "Private turns",
  };
  const segments = spans
    .filter((part) => part.end > part.start)
    .map((part) => {
      const owner =
        part.label ||
        part.slug ||
        part.owner ||
        part.span_owner ||
        part.kind ||
        "Context";
      return {
        name: labels[owner] || owner,
        start: part.start,
        end: part.end,
        len: part.end - part.start,
        hit: Math.max(0, Math.min(cached, part.end) - part.start),
      };
    });
  const bar = (
    <div
      className="prefix-bar"
      role="img"
      aria-label={`${count(cached)} cached of ${count(total)} prompt tokens`}
    >
      {segments.map((part) => (
        <span
          key={part.start}
          style={{ flexGrow: part.len }}
          title={`${part.name}: ${count(part.hit)} of ${count(part.len)} tokens cached`}
        >
          <i style={{ width: `${(100 * part.hit) / part.len}%` }} />
        </span>
      ))}
    </div>
  );
  return (
    <div className="prefix-stack">
      {bar}
      <div className="span-key">
        {segments.map((part) => (
          <span key={part.start}>
            {part.name} <b>{count(part.len)}</b>
          </span>
        ))}
      </div>
      <button
        className="text-button"
        type="button"
        onClick={() => dialog.current?.showModal()}
      >
        Inspect measured segments <ArrowRight size={14} />
      </button>
      <dialog
        ref={dialog}
        aria-label="Measured prefix segments"
        className="segment-dialog"
      >
        <div className="toolbar">
          <h2>Measured prefix segments</h2>
          <button
            className="secondary"
            type="button"
            onClick={() => dialog.current?.close()}
          >
            Close
          </button>
        </div>
        {source && <p>{source}</p>}
        <div className="segment-summary">
          <strong>{count(cached)}</strong> of {count(total)} prompt tokens
          cached
        </div>
        {bar}
        <div className="table-scroller">
          <table>
            <thead>
              <tr>
                <th>Prefix segment</th>
                <th>Token range</th>
                <th className="numeric">Cache hits</th>
                <th className="numeric">Uncached</th>
              </tr>
            </thead>
            <tbody>
              {segments.map((part) => (
                <tr key={part.start}>
                  <td>{part.name}</td>
                  <td>
                    {count(part.start)}–{count(part.end)}
                  </td>
                  <td className="numeric">{count(part.hit)}</td>
                  <td className="numeric">{count(part.len - part.hit)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </dialog>
    </div>
  );
}

function Money({ value }: { value: number | undefined }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (ref.current && !matchMedia("(prefers-reduced-motion: reduce)").matches)
      ref.current.animate(
        [
          { transform: "translateY(5px)", opacity: 0.3 },
          { transform: "none", opacity: 1 },
        ],
        { duration: 240 },
      );
  }, [value]);
  return (
    <span ref={ref} className="money-val">
      {usd(value, value != null && value % 10_000 !== 0 ? 6 : 2)}
    </span>
  );
}
export function MoneyStrip({ mandate }: { mandate: Mandate | null }) {
  const authorized = mandate?.authorization_id ? mandate.cap_micros : undefined;
  return (
    <div className="money-strip">
      {[
        ["Authorized", authorized],
        ["Captured", mandate?.captured_micros],
        ["PayPal fees", mandate?.capture_fee_micros],
        ["Forgiven", mandate?.forgiven_micros],
        ["Released authorization", mandate?.released_micros],
      ].map(([label, value]) => (
        <div key={label} data-testid={"money-" + String(label).toLowerCase().split(" ").at(0)}>
          <small>{label}</small>
          <Money value={typeof value === "number" ? value : undefined} />
        </div>
      ))}
    </div>
  );
}

export function ModeBadge({ health, error }: { health: Health | null; error: string }) {
  const name = health?.mode;
  const label =
    name === "live"
      ? `Live · self-hosted SGLang${health?.gpu ? " ("+health.gpu.replace(/^NVIDIA (?:GeForce )?/, "")+")" : ""}`
      : name === "hosted"
        ? "Live · hosted cache fallback"
        : name === "replay"
          ? "Replay of recorded GPU session"
          : name === "unavailable" || error
            ? "Inference offline"
            : "Checking inference";
  return (
    <span
      className={`mode-dot ${name === "live" || name === "hosted" ? "online" : ""}`}
      title={health?.gpu || health?.model || error || label}
    >
      {label}
    </span>
  );
}
export function FeeHint({amountCents, feeCents, actual = false}: {
  amountCents?: number; feeCents?: number; actual?: boolean;
}) {
  if (amountCents === undefined || feeCents === undefined) return <small className="help">No capture estimate yet.</small>;
  return <p className="help" data-testid="fee-hint">
    {actual ? "Collected" : "Estimated collection"} {usd(amountCents*10_000,2)} · {actual ? "actual fee" : "estimated fee"} {usd(feeCents*10_000,2)} · net {usd((amountCents-feeCents)*10_000,2)}
  </p>;
}
