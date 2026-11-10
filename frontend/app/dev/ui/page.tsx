"use client";
import {
  Empty,
  Failure,
  FeeHint,
  ModeBadge,
  MoneyStrip,
  PageTitle,
  Skeleton,
  Tag,
} from "../../../components/ui";
import { ArchivedPrefix } from "../../../components/archive";
import { useResource } from "../../../lib/client";
import type { Health, State } from "../../../lib/types";
export default function ComponentGallery() {
  const state = useResource<State>("judge/state");
  const health = useResource<Health>("health");
  const mandate = state.data?.kind === "judge" ? state.data.mandate : null;
  const preview = useResource<{amount_cents:number;estimated_fee_cents:number}>(
    mandate ? "judge/capture-preview?mandate_id="+encodeURIComponent(mandate.id) : null
  );
  return (
    <>
      <PageTitle title="Interface components">
        Shared states and workspace components.
      </PageTitle>
      <div className="two-col">
        <section className="panel stack">
          <h2>Feedback</h2>
          <div className="actions">
            <Tag>Waiting</Tag>
            <Tag tone="good">Hot</Tag>
            <Tag tone="warn">Warming</Tag>
            <Tag tone="bad">Failed</Tag>
          </div>
          <h3>Loading state</h3>
          <Skeleton />
          <Empty title="Empty state" />
          <Failure error="Error presentation preview" />
        </section>
        <section className="panel stack">
          <h2>Inference mode</h2>
          <ModeBadge health={health.data} error={health.error}/>
          <h2>Capture fee</h2>
          <FeeHint amountCents={preview.data?.amount_cents} feeCents={preview.data?.estimated_fee_cents}/>
          <h2>Money strip</h2>
          <MoneyStrip mandate={state.data?.mandate || null} />
          <h2>Prefix stack</h2>
          <ArchivedPrefix expanded />
        </section>
      </div>
    </>
  );
}
