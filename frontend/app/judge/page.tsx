import { PageTitle } from "../../components/ui";
import { Judge } from "../../components/judge";
export const metadata = { title: "Judge mode" };
export default function Page() {
  return (
    <>
      <PageTitle title="Judge mode">
        Walk through publishing, agent spending and settlement in an isolated
        sandbox workspace.
      </PageTitle>
      <Judge />
    </>
  );
}
