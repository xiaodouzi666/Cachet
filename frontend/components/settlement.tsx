"use client";
import { useCallback, useMemo } from "react";
import dynamic from "next/dynamic";
import { RefreshCw } from "lucide-react";
import { useResource } from "../lib/client";
import { Empty, Failure, Skeleton, Tag } from "./ui";
import type { FinancialRow, Reconcile, Royalty } from "../lib/types";
import { useSession } from "./shell";
import { SettlementPayout, type PayoutBatch } from "./settlement-payout";

const Studio = dynamic(() => import("../studio/settlement"), {
  ssr: false,
  loading: () => <Skeleton label="Loading AG Studio" />,
});
export function Settlement() {
  const session = useSession();
  const ledger = useResource<FinancialRow[]>("ledger", 12000),
    captures = useResource<FinancialRow[]>("captures", 12000),
    refunds = useResource<FinancialRow[]>("refunds", 12000),
    payouts = useResource<PayoutBatch[]>("payouts", 12000),
    events = useResource<FinancialRow[]>("paypal-events", 12000),
    system = useResource<{ hit_tokens: number }>("royalties/system", 12000),
    royalties = useResource<Royalty[]>("royalties", 12000),
    recon = useResource<Reconcile>("reconcile");
  const resources = [ledger, captures, refunds, payouts, events, royalties, system];
  const tables = useMemo(
    () => ({
      ledger: ledger.data || [],
      captures: captures.data || [],
      refunds: refunds.data || [],
      payouts: payouts.data || [],
      events: events.data || [],
    }),
    [ledger.data, captures.data, refunds.data, payouts.data, events.data],
  );
  const refresh = useCallback(() => {
    [ledger, captures, refunds, payouts, events, royalties, system, recon].forEach(
      (r) => void r.reload(),
    );
  }, [ledger, captures, refunds, payouts, events, royalties, system, recon]);
  const err = resources.find((r) => r.error)?.error || "";
  return (
    <div className="stack">
      {session.state?.kind === "judge" && <SettlementPayout batches={payouts.data ?? []} refresh={refresh} />}
      <section className="panel">
        <div className="panel-head">
          <div>
            <h2>Reconciliation</h2>
            <p>
              Compare this workspace’s recorded objects with PayPal sandbox
              readbacks.
            </p>
          </div>
          <button className="secondary" onClick={recon.reload}>
            <RefreshCw size={15} />
            Reconcile
          </button>
        </div>
        {recon.loading ? (
          <Skeleton />
        ) : recon.error ? (
          <Failure error={recon.error} retry={recon.reload} />
        ) : recon.data && recon.data.checked > 0 ? (
          <>
            <div className="actions">
              <Tag tone={recon.data.ok ? "good" : "bad"}>
                {recon.data.ok ? "Matched" : "Differences detected"}
              </Tag>
              <small>{recon.data.checked} objects checked</small>
            </div>
            {!recon.data.ok && (
              <pre className="receipt">
                {JSON.stringify(recon.data.differences, null, 2)}
              </pre>
            )}
          </>
        ) : (
          <Empty title="No PayPal objects reconciled yet" />
        )}
      </section>
      {err && <Failure error={err} retry={refresh} />}
      {resources.some((r) => r.loading) ? (
        <Skeleton label="Loading workspace statements" />
      ) : (
        !err && (
          <Studio
            tables={tables}
            royalties={royalties.data || []}
            systemHits={system.data?.hit_tokens ?? null}
            afterConfirm={refresh}
          />
        )
      )}
    </div>
  );
}
