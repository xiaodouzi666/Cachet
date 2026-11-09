"use client";
import { useState } from "react";
import { useSession } from "./shell";
import { BookOpen, Thermometer } from "lucide-react";
import { api, count, describe, useResource } from "../lib/client";
import type { ContextRow, Source } from "../lib/types";
import { ActionLink, Empty, Failure, Skeleton, Tag } from "./ui";

export function Publisher() {
  const session = useSession();
  const replay = session.mode === "replay";
  const sources = useResource<Source[]>("sources"),
    catalog = useResource<ContextRow[]>("contexts", 5000);
  const [name, setName] = useState(""),
    [email, setEmail] = useState(""),
    [slug, setSlug] = useState(""),
    [parent, setParent] = useState("");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [submitted, setSubmitted] = useState("");
  const [publisher, setPublisher] = useState<{
    signature: string;
    id: string;
  } | null>(null);
  const probe = useResource<ContextRow>(
    submitted ? `contexts/${encodeURIComponent(submitted)}` : null,
    2000,
  );
  const source =
    sources.data?.find((s) => s.slug === slug) || sources.data?.[0];
  const parents = catalog.data?.filter((r) => r.slug === source?.parent);
  const chosenParent =
    parents?.find((r) => r.id === parent)?.id || parents?.[0]?.id;
  async function publish(e: React.FormEvent) {
    e.preventDefault();
    if (!source) return;
    setError("");
    setBusy(true);
    try {
      const signature = JSON.stringify([name.trim(), email.trim()]);
      let publisherId =
        publisher?.signature === signature ? publisher.id : undefined;
      if (!publisherId) {
        publisherId = (
          await api<{ id: string }>("publishers", {
            name: name.trim(),
            paypal_email: email.trim(),
          })
        ).id;
        setPublisher({ signature, id: publisherId });
      }
      const row = await api<{ id: string }>("contexts", {
        publisher_id: publisherId,
        source_slug: source.slug,
        parent_id: source.parent ? chosenParent : null,
      });
      setSubmitted(row.id);
      void catalog.reload();
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(false);
    }
  }
  async function warm() {
    setBusy(true);
    setError("");
    try {
      await api(`contexts/${encodeURIComponent(submitted)}/warm`, {});
      await probe.reload();
    } catch (e) {
      setError(describe(e));
    } finally {
      setBusy(false);
    }
  }
  const optionError = sources.error || catalog.error;
  return (
    <div className="two-col">
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Publisher and source</h2>
            <p>
              The receiver email identifies the PayPal sandbox payout account.
            </p>
          </div>
          <BookOpen size={21} />
        </div>
        <Failure
          error={optionError}
          retry={() => {
            void sources.reload();
            void catalog.reload();
          }}
        />
        {sources.loading ? (
          <Skeleton />
        ) : !source && !optionError ? (
          <Empty title="No licensed sources available" />
        ) : (
          <form onSubmit={publish} className="form-grid">
            <label className="field">
              Publisher name
              <input
                required
                autoComplete="organization"
                value={name}
                maxLength={120}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <label className="field">
              PayPal receiver email
              <input
                required
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </label>
            <label className="field full">
              Licensed source
              <select
                value={source?.slug || ""}
                onChange={(e) => {
                  setSlug(e.target.value);
                  setParent("");
                }}
              >
                {sources.data?.map((s) => (
                  <option key={s.slug} value={s.slug}>
                    {s.title || s.slug} · {count(s.token_len)} tokens
                  </option>
                ))}
              </select>
            </label>
            {source && session.state?.publisher_receivers?.[source.slug] && (
              <div className="full">
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    setName((source.title || source.slug) + " Maintainer");
                    setEmail(
                      session.state?.publisher_receivers?.[source.slug] || "",
                    );
                  }}
                >
                  Use configured sandbox receiver for{" "}
                  {source.title || source.slug}
                </button>
              </div>
            )}
            {source?.parent && (
              <label className="field full">
                Parent context
                <select
                  required
                  value={chosenParent || ""}
                  onChange={(e) => setParent(e.target.value)}
                >
                  <option value="" disabled>
                    Publish {source.parent} first
                  </option>
                  {parents?.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.title} · {r.publisher_name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <div className="actions full">
              <button
                className="primary"
                disabled={
                  busy ||
                  !session.mode ||
                  !source ||
                  (!!source.parent && !chosenParent)
                }
              >
                {busy
                  ? "Processing…"
                  : replay
                    ? "Register for replay"
                    : "Publish and warm"}
              </button>
            </div>
          </form>
        )}
        <Failure error={error} />
      </section>
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>{replay ? "Recorded workload" : "Engine probe"}</h2>
            <p>
              {replay
                ? "Register the licensed layers to use verified recorded workloads. Live warming and hit probes are unavailable."
                : "The serving engine measures the reusable prefix."}
            </p>
          </div>
          <Thermometer size={21} />
        </div>
        {!submitted ? (
          <Empty title="Awaiting publication">
            {replay
              ? "The registered layer will use the stored GPU measurement."
              : "Your measured hit ratio appears after warm-up."}
          </Empty>
        ) : probe.error ? (
          <Failure error={probe.error} retry={probe.reload} />
        ) : !probe.data ? (
          <Skeleton />
        ) : (
          <div className="stack">
            <div className="toolbar">
              <b>{probe.data.title}</b>
              <Tag
                tone={
                  replay
                    ? "neutral"
                    : probe.data.status === "hot"
                      ? "good"
                      : "warn"
                }
              >
                {replay ? "Registered for replay" : probe.data.status}
              </Tag>
            </div>
            {!replay && probe.data.last_probe_hit_ratio != null ? (
              <div className="probe-result">
                <strong>
                  {(probe.data.last_probe_hit_ratio * 100).toFixed(2)}%
                </strong>
                <span>measured prefix hit ratio</span>
              </div>
            ) : (
              <p role="status">
                {replay
                  ? "Live probe unavailable. Recorded turns use the registered source chain."
                  : probe.data.status === "cold"
                    ? "The live probe did not verify a warm prefix."
                    : "Waiting for a completed probe."}
              </p>
            )}
            <small>
              Published layer · {count(probe.data.token_len)} tokens
            </small>
            <div className="actions">
              <ActionLink href="/catalog" quiet>
                View catalog
              </ActionLink>
              {!replay &&
                probe.data.can_manage !== false &&
                probe.data.status !== "warming" && (
                  <button className="secondary" disabled={busy} onClick={warm}>
                    Warm again
                  </button>
                )}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
