"use client";
import { useCallback, useEffect, useRef, useState } from "react";

export function describe(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
export async function check(response: Response) {
  if (response.ok) return response;
  const data = await response.json().catch(() => ({}));
  const detail = data.detail ?? data.message ?? response.statusText;
  const message =
    typeof detail === "string"
      ? detail
      : Array.isArray(detail)
        ? detail
            .map((item: { msg?: string }) => item.msg)
            .filter(Boolean)
            .join(", ")
        : detail.message || detail.detail || response.statusText;
  const debug =
    response.headers.get("paypal-debug-id") ||
    data.paypal_debug_id ||
    data.debug_id ||
    detail?.debug_id;
  const retry = response.headers.get("retry-after");
  throw new Error(
    `${message || "Request failed"} (${response.status})${debug ? ` · PayPal debug ID ${debug}` : ""}${retry ? ` · Retry after ${retry}s` : ""}`,
  );
}
export async function api<T>(
  path: string,
  data?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await check(
    await fetch(`/api/${path}`, {
      method: data === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      signal,
      headers:
        data === undefined ? undefined : { "Content-Type": "application/json" },
      body: data === undefined ? undefined : JSON.stringify(data),
    }),
  );
  return response.status === 204
    ? (null as T)
    : (response.json() as Promise<T>);
}

let opening: { key: string; result: Promise<void> } | null = null;
export function workspace(
  kind: "judge" | "user",
  switchBack = false,
): Promise<void> {
  const key = kind + (switchBack ? "-switch" : "");
  if (opening?.key === key) return opening.result;
  const previous = opening?.result.catch(() => {});
  const result = Promise.resolve(previous)
    .then(() =>
      api("judge/workspace", { kind, ...(switchBack ? { switch: true } : {}) }),
    )
    .then(() => undefined)
    .finally(() => {
      if (opening?.result === result) opening = null;
    });
  opening = { key, result };
  return result;
}

export function useResource<T>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const active = useRef<AbortController | null>(null);
  const reload = useCallback(async () => {
    active.current?.abort();
    if (!path) return;
    const controller = new AbortController();
    active.current = controller;
    try {
      const result = await api<T>(path, undefined, controller.signal);
      if (!controller.signal.aborted) {
        setData(result);
        setError("");
      }
    } catch (e) {
      if (!controller.signal.aborted) setError(describe(e));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    setData(null);
    setError("");
    setLoading(Boolean(path));
    void reload();
    const timer =
      interval && path
        ? window.setInterval(() => {
            if (!document.hidden) void reload();
          }, interval)
        : null;
    return () => {
      active.current?.abort();
      if (timer) window.clearInterval(timer);
    };
  }, [path, reload, interval]);
  return { data, error, loading, reload };
}
export function usd(micros: number | null | undefined, digits = 6) {
  if (micros == null || !Number.isFinite(micros)) return "—";
  return (micros / 1_000_000).toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  });
}
export function count(value: number | undefined | null) {
  return value == null ? "—" : value.toLocaleString("en-US");
}
export function latency(ms: number | null | undefined) {
  return ms == null
    ? "—"
    : ms < 1000
      ? `${ms.toFixed(1)} ms`
      : `${(ms / 1000).toFixed(2)} s`;
}
