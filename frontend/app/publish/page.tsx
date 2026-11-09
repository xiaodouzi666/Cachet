import { WorkspaceGate } from "../../components/shell";
import { Publisher } from "../../components/publisher";
import { PageTitle } from "../../components/ui";
export const metadata = { title: "Publish context" };
export default function Page() {
  return (
    <>
      <PageTitle title="Publish context">
        Register a maintainer and publish a licensed context layer.
      </PageTitle>
      <WorkspaceGate>
        <Publisher />
      </WorkspaceGate>
    </>
  );
}
