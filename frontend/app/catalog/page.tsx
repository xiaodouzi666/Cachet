import { ActionLink, PageTitle } from "../../components/ui";
import { WorkspaceGate } from "../../components/shell";
import { Catalog } from "../../components/catalog";
export const metadata = { title: "Context catalog" };
export default function Page() {
  return (
    <>
      <PageTitle
        title="Context catalog"
        action={<ActionLink href="/publish">Publish context</ActionLink>}
      >
        Explore published prefixes, cache state and metered publisher royalties.
      </PageTitle>
      <WorkspaceGate>
        <Catalog />
      </WorkspaceGate>
    </>
  );
}
