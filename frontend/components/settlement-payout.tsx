"use client";

import { useState } from "react";
import { api, describe, usd, useResource } from "../lib/client";
import { Failure, Skeleton } from "./ui";

type PayoutItem = {
  id: string;
  paypal_item_id: string | null;
  publisher_id: string;
  status: string;
  amount_cents: number;
  fee_cents: number | null;
  net_cents: number | null;
};
export type PayoutBatch = Record<string, unknown> & {
  id: string;
  paypal_batch_id: string | null;
  status: string;
  items: PayoutItem[];
};
type Preview = {
  quote_id: string;
  batch_id: string | null;
  minimum_micros: number;
  amount_cents: number;
  items: { publisher_id: string; name: string; amount_cents: number }[];
};
const dollars = (amount: number | null) => usd(amount === null ? null : amount * 10000, 2);

export function SettlementPayout({ batches, refresh }: { batches: PayoutBatch[]; refresh: () => void }) {
  const plan = useResource<Preview>("judge/settle", 12000);
  const [review, setReview] = useState<Preview | null>(null);
  const [selectedId, select] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const batch = batches.find(b => b.id === selectedId) ?? batches[0];
  async function submit(body: { batch_id: string | null; quote_id?: string }) {
    setBusy(true);
    setError("");
    try {
      const result = await api<{ id: string }>("judge/settle", body);
      select(result.id);
    } catch (e) {
      setError(describe(e));
    } finally {
      setReview(null);
      setBusy(false);
      void plan.reload();
      refresh();
    }
  }
  return (
    <section className="panel stack" aria-label="Judge payout">
      <div className="panel-head">
        <div>
          <h2>Publisher payout</h2>
          {plan.data && <p>{dollars(plan.data.amount_cents)} eligible · minimum {usd(plan.data.minimum_micros, 2)} per publisher</p>}
        </div>
        <button type="button" className="secondary" disabled={busy || !!review || !plan.data?.items.length}
          onClick={() => { setError(""); setReview(plan.data); }}>
          Settle now
        </button>
      </div>
      {plan.loading && <Skeleton label="Checking funded royalties" />}
      <Failure error={error || plan.error} retry={plan.reload} />
      {review && (
        <div className="stack" role="group" aria-label="Review sandbox payout">
          <strong>Send {dollars(review.amount_cents)} through PayPal Sandbox?</strong>
          <ul>{review.items.map(p => <li key={p.publisher_id}>{p.name} · {dollars(p.amount_cents)}</li>)}</ul>
          <div className="actions">
            <button className="primary" disabled={busy} onClick={() => void submit({ batch_id: review.batch_id, quote_id: review.quote_id })}>
              Confirm sandbox payout
            </button>
            <button className="secondary" disabled={busy} onClick={() => setReview(null)}>Cancel</button>
          </div>
        </div>
      )}
      {batch?.paypal_batch_id && (
        <div className="stack" data-testid="settlement-batch" data-batch-id={batch.paypal_batch_id}>
          <div className="panel-head">
            <div><strong>PayPal batch</strong> <code>{batch.paypal_batch_id}</code> · <span data-field="status">{batch.status}</span></div>
            <button type="button" className="text-button" disabled={busy || !!review}
              onClick={() => void submit({ batch_id: batch.id })}>Refresh batch status</button>
          </div>
          <div className="table-scroller">
            <table><thead><tr><th>Payout item</th><th>Publisher</th><th>Status</th><th className="numeric">Amount</th><th className="numeric">PayPal fee</th><th className="numeric">Total debited</th></tr></thead>
              <tbody>{batch.items.map(item => (
                <tr key={item.id} data-testid="settlement-item" data-item-id={item.paypal_item_id ?? ""}>
                  <td><code>{item.paypal_item_id ?? "Awaiting PayPal ID"}</code></td>
                  <td><code>{item.publisher_id}</code></td>
                  <td data-field="status">{item.status}</td>
                  <td className="numeric" data-field="amount_cents">{dollars(item.amount_cents)}</td>
                  <td className="numeric" data-field="fee_cents">{dollars(item.fee_cents)}</td>
                  <td className="numeric" data-field="net_cents">{dollars(item.net_cents)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
