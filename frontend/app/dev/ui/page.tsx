"use client";
import {
  Empty,
  Failure,
  MoneyStrip,
  PageTitle,
  Skeleton,
  Tag,
} from "../../../components/ui";
import { ArchivedPrefix } from "../../../components/archive";
import { useResource } from "../../../lib/client";
import type { State } from "../../../lib/types";
export default function ComponentGallery() {
  const state = useResource<State>("judge/state");
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
          <h2>Money strip</h2>
          <MoneyStrip mandate={state.data?.mandate || null} />
          <h2>Prefix stack</h2>
          <ArchivedPrefix expanded />
        </section>
      </div>
    </>
  );
}
