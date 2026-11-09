import { WorkspaceGate } from "../../components/shell";
import { PageTitle } from "../../components/ui";
import { Settlement } from "../../components/settlement";
export const metadata = { title: "Settlement" };
export default function Page() {
  return (
    <>
      <PageTitle title="Settlement">
        Inspect royalties, reconcile PayPal objects and ask the Ops Copilot
        about your workspace.
      </PageTitle>
      <WorkspaceGate>
        <Settlement />
      </WorkspaceGate>
    </>
  );
}
