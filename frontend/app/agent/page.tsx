import { WorkspaceGate } from "../../components/shell";
import { Agent } from "../../components/agent";
import { PageTitle } from "../../components/ui";
export const metadata = { title: "Agent console" };
export default function Page() {
  return (
    <>
      <PageTitle title="Agent console">
        Give a coding agent a budget and follow each metered turn.
      </PageTitle>
      <WorkspaceGate>
        <Agent />
      </WorkspaceGate>
    </>
  );
}
