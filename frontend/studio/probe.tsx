"use client";

import { AgStudio, AgStudioProvider, createWidgets } from "ag-studio-react";
import {
  AgStudioAiModule, createAiHarness, studioTheme,
  type AgAiHarnessSetup, type AgBaseWidgetDefinition, type AgDataSourcesDefinition,
  type AgDefaultRegistry, type AgDefaultWidgetDefinition, type AgReportState,
  type AgStudioApi, type AgWidgetParams,
} from "ag-studio";
import manifest from "../package.json";

const packages = Object.entries(manifest.dependencies).map(([name, version]) => ({ name, version }));
const data: AgDataSourcesDefinition<ProbeRegistry> = {
  sources: [{
    id: "packages",
    name: "Frontend packages",
    data: packages,
    fields: [
      { id: "name", name: "Package name", format: "textFormat" },
      { id: "version", name: "Declared version", format: "textFormat" },
    ],
  }],
};

function SourceInspector({ widgetApi }: AgWidgetParams) {
  const tables = widgetApi.getSchema();
  return (
    <div style={{ padding: "1rem" }}>
      <h2 style={{ marginTop: 0 }}>Source inspector</h2>
      {tables.map(table => (
        <details key={table.id} open>
          <summary>{table.name}</summary>
          <ul>{table.fields.map(field => <li key={field.fieldId}>{field.name}</li>)}</ul>
        </details>
      ))}
    </div>
  );
}

type Inspector = AgBaseWidgetDefinition<"source-inspector">;
interface ProbeRegistry extends Omit<AgDefaultRegistry, "widgets"> {
  widgets: readonly (AgDefaultWidgetDefinition | Inspector)[];
}
const widgets = createWidgets<ProbeRegistry>({
  additionalTypes: [{
    id: "source-inspector",
    label: "Source inspector",
    comp: SourceInspector,
    defaultSize: { width: 45, height: 32 },
    minSize: { width: 26, height: 22 },
    form: () => [],
  }],
});
const state: AgReportState<ProbeRegistry> = {
  pages: [{
    id: "runtime",
    widgets: { inspector: { type: "source-inspector" } },
    widgetLayout: { inspector: { xTrack: 0, yTrack: 0, xSpan: 24, ySpan: 30 } },
  }],
  selectedPageId: "runtime",
  panels: { filters: { collapsed: true }, data: { collapsed: true }, compose: { collapsed: true } },
};

const ai: AgAiHarnessSetup = ({ api: coreApi }) => {
  // Studio 3.1 types this hook with the core API even when the AI module is registered.
  const api = coreApi as AgStudioApi;
  return createAiHarness(api, () => ({
    primary: "inspector",
    promptStarters: [{ label: "Inspect sources", prompt: "Which data sources are connected?" }],
    agents: [{
      id: "inspector",
      name: "Package inspector",
      description: "A local, rule-based inspector for the current schema and frontend package versions.",
      async *run({ threadId, runId, messages }) {
        yield { type: "RUN_STARTED", threadId, runId };
        const question = [...messages].reverse().find(message => message.role === "user")?.content.toLowerCase() ?? "";
        let reply = "Ask which sources are connected, or ask for the version of a frontend package.";
        if (/sources?|tables?|fields?|schema/.test(question)) {
          reply = api.getAiContext().schema().tables
            .map(table => `${table.name} has fields ${table.fields.map(field => field.name).join(", ")}.`)
            .join(" ") || "The current dashboard has no connected sources.";
        } else if (/versions?|packages?/.test(question)) {
          const requested = packages.filter(({ name }) => question.split(/[^\w-]+/).includes(name));
          const listed = requested.length ? requested : packages;
          reply = `Declared dependencies: ${listed.map(pkg => `${pkg.name} ${pkg.version}`).join(", ")}.`;
        }
        const messageId = `${runId}-answer`;
        yield { type: "TEXT_MESSAGE_START", messageId, role: "assistant" };
        yield { type: "TEXT_MESSAGE_CONTENT", messageId, delta: reply };
        yield { type: "TEXT_MESSAGE_END", messageId };
        yield { type: "RUN_FINISHED", threadId, runId };
      },
    }],
  }));
};

const theme = studioTheme.withParams({ accentColor: "#0F766E", backgroundColor: "#FFFFFF", textColor: "#111827" });
const modules = [AgStudioAiModule];

export default function Probe() {
  return (
    <AgStudioProvider modules={modules}>
      <AgStudio<ProbeRegistry> style={{ height: "72vh", minHeight: 480 }}
        data={data} theme={theme} widgets={widgets} initialState={state} ai={ai} mode="edit" />
    </AgStudioProvider>
  );
}
