import { expect, test } from "@playwright/test";
import { hasRunnableLayers } from "../../lib/judge";
import type { Source, Span } from "../../lib/types";

test("the verified archive's licensed chain is eligible offline while a live run needs hot probes", async ({
  request,
}) => {
  const catalog = await request.get("/api/sources");
  const archive = await request.get("/api/reference-turn");
  expect(catalog.ok()).toBe(true);
  expect(archive.ok()).toBe(true);
  const sources: Source[] = await catalog.json();
  const { mode, spans } = (await archive.json()) as {
    mode: string;
    spans: Span[];
  };
  expect(mode).toBe("recorded");
  const owners = new Set(
    spans.filter((span) => span.kind === "publisher").map((span) => span.owner),
  );
  // Exercise the readiness predicate against the archive's real source relationships.
  // Keys here are manifest slugs. This test creates no workspace, publisher or payment.
  const chain = sources
    .filter((source) => owners.has(source.slug))
    .map((source) => ({
      id: source.slug,
      slug: source.slug,
      parent_id: source.parent,
      status: "cold",
    }));
  expect(hasRunnableLayers(chain, "replay")).toBe(true);
  expect(hasRunnableLayers(chain, "live")).toBe(false);
  expect(hasRunnableLayers(chain, "unavailable")).toBe(false);
  expect(
    hasRunnableLayers(
      chain.filter((source) => source.parent_id == null),
      "replay",
    ),
  ).toBe(false);
});
