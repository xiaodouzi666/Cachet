"use client";
import { createContext, useContext, useMemo, useRef, useState } from "react";
import { AgStudio, AgStudioProvider, createWidgets } from "ag-studio-react";
import {
  AgStudioAiModule,
  createAiHarness,
  studioTheme,
  type AgAiHarnessSetup,
  type AgBaseWidgetDefinition,
  type AgDataSourcesDefinition,
  type AgDefaultRegistry,
  type AgDefaultWidgetDefinition,
  type AgReportState,
  type AgStudioApi,
} from "ag-studio";
import { GitBranch } from "lucide-react";
import { api, count, describe, usd } from "../lib/client";
import type { FinancialRow, Royalty } from "../lib/types";
import { copilotEvents } from "../lib/stream";
import { Empty, Failure } from "../components/ui";

const sources = [
  {
    id: "ledger",
    name: "Ledger",
    fields: [
      "txn_id",
      "account",
      "debit_micros",
      "credit_micros",
      "ref_type",
      "ref_id",
      "created_at",
    ],
  },
  {
    id: "captures",
    name: "Captures",
    fields: [
      "paypal_capture_id",
      "mandate_id",
      "amount_cents",
      "fee_cents",
      "status",
    ],
  },
  {
    id: "refunds",
    name: "Refunds",
    fields: [
      "paypal_refund_id",
      "capture_id",
      "amount_cents",
      "reason",
      "status",
    ],
  },
  {
    id: "payouts",
    name: "Payout batches",
    fields: ["paypal_batch_id", "sender_batch_id", "fee_cents", "status"],
  },
] as const;
type SourceId = (typeof sources)[number]["id"];
export type Tables = Record<SourceId, FinancialRow[]>;
type Proposal = { proposal_id: string; description: string };
const Royalties = createContext<Royalty[]>([]);
function RoyaltyTree() {
  const rows = useContext(Royalties),
    [basis, setBasis] = useState<"linear" | "shapley">("shapley");
  function branch(row: Royalty, ancestors: Set<string>): React.ReactNode {
    if (ancestors.has(row.id)) return null;
    const path = new Set(ancestors).add(row.id),
      next = rows.filter((child) => child.parent_id === row.id);
    return (
      <li key={row.id}>
        <div>
          <GitBranch size={15} />
          <span>{row.title}</span>
          <small>{count(row.hit_tokens)} hits</small>
          <b>
            {usd(basis === "linear" ? row.linear_micros : row.shapley_micros)}
          </b>
        </div>
        {next.length > 0 && <ul>{next.map((child) => branch(child, path))}</ul>}
      </li>
    );
  }
  return (
    <div className="royalty-widget">
      <h3>Prefix Royalty Tree</h3>
      <div className="tabs">
        {(["shapley", "linear"] as const).map((k) => (
          <button
            type="button"
            key={k}
            onClick={() => setBasis(k)}
            aria-pressed={k === basis}
          >
            {k === "linear" ? "Linear" : "Shapley"}
          </button>
        ))}
      </div>
      {rows.length ? (
        <ul>
          {rows
            .filter(
              (r) => !r.parent_id || !rows.some((p) => p.id === r.parent_id),
            )
            .map((r) => branch(r, new Set()))}
        </ul>
      ) : (
        <Empty title="No attributed hits in this workspace" />
      )}
    </div>
  );
}
interface Registry extends Omit<AgDefaultRegistry, "widgets"> {
  widgets: readonly (
    | AgDefaultWidgetDefinition
    | AgBaseWidgetDefinition<"royalty-tree">
  )[];
}
const widgets = createWidgets<Registry>({
  additionalTypes: [
    {
      id: "royalty-tree",
      label: "Prefix Royalty Tree",
      comp: RoyaltyTree,
      defaultSize: { width: 50, height: 30 },
      minSize: { width: 30, height: 22 },
      form: () => [],
    },
  ],
});
const layout = { minWidth: 250, columns: 24 };
const state: AgReportState<Registry> = {
  pages: [
    {
      id: "Royalties",
      layout,
      widgets: { tree: { type: "royalty-tree" } },
      widgetLayout: { tree: { xTrack: 0, yTrack: 0, xSpan: 24, ySpan: 32 } },
    },
    ...sources.map((src) => ({
      id: src.name,
      layout,
      widgets: {
        entries: {
          type: "grid" as const,
          dataMapping: {
            cols: src.fields.map((field) => ({ id: `${src.id}.${field}` })),
          },
          format: { title: { enabled: true, text: src.name } },
        },
      },
      widgetLayout: { entries: { xTrack: 0, yTrack: 0, xSpan: 24, ySpan: 32 } },
    })),
  ],
  selectedPageId: "Royalties",
  panels: { ai: { collapsed: true, width: 290 } },
};
const modules = [AgStudioAiModule];
const theme = studioTheme.withParams({
  accentColor: "#0f766e",
  backgroundColor: "#ffffff",
  textColor: "#111827",
});
export default function SettlementStudio({
  tables,
  royalties,
  afterConfirm,
}: {
  tables: Tables;
  royalties: Royalty[];
  afterConfirm: () => void;
}) {
  const studioRef = useRef<AgStudioApi<Registry> | null>(null);
  const [active, setActive] = useState("Royalties");
  const [proposal, setProposal] = useState<Proposal | null>(null),
    [confirming, setConfirming] = useState(false),
    [error, setError] = useState(""),
    [receipt, setReceipt] = useState<unknown>(null);
  const data = useMemo<AgDataSourcesDefinition<Registry>>(
    () => ({
      sources: sources.map((src) => ({
        id: src.id,
        name: src.name,
        data: tables[src.id].map((record) =>
          Object.fromEntries(
            src.fields.map((key) => [key, record[key] ?? null]),
          ),
        ),
        fields: src.fields.map((key) => ({
          id: key,
          name: key.replaceAll("_", " "),
          format: /(micros|cents)$/.test(key) ? "integerFormat" : "textFormat",
        })),
      })),
    }),
    [tables],
  );
  const ai = useMemo<AgAiHarnessSetup>(
    () =>
      ({ api: core }) =>
        createAiHarness(core as AgStudioApi, () => ({
          primary: "ops",
          promptStarters: [
            {
              label: "Explain royalties",
              prompt:
                "How much did Starlette earn this week, and why more per token than FastAPI?",
            },
          ],
          agents: [
            {
              id: "ops",
              name: "Ops Copilot",
              description:
                "Investigates this workspace's recorded payments and proposes refunds for approval.",
              async *run({ threadId, runId, messages }, { signal }) {
                yield { type: "RUN_STARTED", threadId, runId };
                const question = [...messages]
                  .reverse()
                  .find((m) => m.role === "user")?.content;
                const messageId = `${runId}-reply`;
                yield {
                  type: "TEXT_MESSAGE_START",
                  messageId,
                  role: "assistant",
                };
                try {
                  if (!question)
                    throw new Error("Enter a question about the workspace.");
                  let answered = false,
                    finished = false;
                  for await (const { event, eventData: d } of copilotEvents(
                    question,
                    signal,
                  )) {
                    if (event === "error")
                      throw new Error(
                        d.message || d.detail || "The Copilot request failed.",
                      );
                    if (event === "proposal") {
                      if (!d.proposal_id)
                        throw new Error(
                          "The refund proposal has no confirmation ID.",
                        );
                      setProposal(d);
                      setReceipt(null);
                    }
                    const text =
                      event === "proposal"
                        ? d.description
                        : event === "delta" || event === "message"
                          ? d.text
                          : undefined;
                    if (text) {
                      answered = true;
                      yield {
                        type: "TEXT_MESSAGE_CONTENT",
                        messageId,
                        delta: text,
                      };
                    }
                    if (event === "done") finished = true;
                  }
                  if (!answered || !finished)
                    throw new Error(
                      "The answer stream ended before completion.",
                    );
                } catch (e) {
                  if (signal?.aborted) return;
                  yield {
                    type: "TEXT_MESSAGE_CONTENT",
                    messageId,
                    delta: `\nUnable to complete the query. ${describe(e)}`,
                  };
                }
                yield { type: "TEXT_MESSAGE_END", messageId };
                yield { type: "RUN_FINISHED", threadId, runId };
              },
            },
          ],
        })),
    [],
  );
  async function confirm() {
    if (!proposal) return;
    setConfirming(true);
    setError("");
    try {
      const r = await api("copilot/confirm", {
        proposal_id: proposal.proposal_id,
      });
      setReceipt(r);
      setProposal(null);
      afterConfirm();
    } catch (e) {
      setError(describe(e));
    } finally {
      setConfirming(false);
    }
  }
  return (
    <>
      <section className="studio-shell">
        <nav className="statement-nav" aria-label="Settlement statements">
          {state.pages.map((page) => (
            <button
              type="button"
              key={page.id}
              aria-current={active === page.id ? "page" : undefined}
              onClick={() => {
                const studio = studioRef.current;
                if (studio)
                  studio.setState({
                    ...studio.getState(),
                    selectedPageId: page.id,
                  });
              }}
            >
              {page.id}
            </button>
          ))}
        </nav>
        <Royalties.Provider value={royalties}>
          <AgStudioProvider
            modules={modules}
            licenseKey={
              process.env.NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY || undefined
            }
          >
            <AgStudio<Registry>
              onStudioReady={(e) => {
                studioRef.current = e.api as AgStudioApi<Registry>;
              }}
              onStateUpdated={(e) =>
                setActive(
                  (e.api as AgStudioApi<Registry>).getState().selectedPageId,
                )
              }
              style={{ height: "max(560px,74vh)" }}
              data={data}
              widgets={widgets}
              theme={theme}
              initialState={state}
              ai={ai}
              mode="edit"
              panels={{ edit: { left: [], right: ["ai"] } }}
              localeText={{ panelTitleAi: "Ops Copilot" }}
            />
          </AgStudioProvider>
        </Royalties.Provider>
      </section>
      {proposal && (
        <aside className="panel">
          <h3>Confirm refund</h3>
          <p>{proposal.description}</p>
          <small>
            Proposal <code>{proposal.proposal_id}</code>
          </small>
          <div className="actions">
            <button className="primary" onClick={confirm} disabled={confirming}>
              Confirm sandbox refund
            </button>
            <button
              className="secondary"
              onClick={() => setProposal(null)}
              disabled={confirming}
            >
              Dismiss
            </button>
          </div>
        </aside>
      )}
      <Failure error={error} />
      {receipt != null && (
        <pre className="receipt">{JSON.stringify(receipt, null, 2)}</pre>
      )}
    </>
  );
}
