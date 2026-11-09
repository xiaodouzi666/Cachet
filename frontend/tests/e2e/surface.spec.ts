import { test, expect } from "@playwright/test";

test("uses the real API through isolated browser workspaces and the settlement desk", async ({
  page,
  context,
}) => {
  const exceptions: string[] = [];
  page.on("pageerror", (e) => exceptions.push(e.message));
  await page.goto("/?rec=1");
  await expect(page.locator("html")).toHaveClass(/recording/);
  await page.getByRole("heading", { name: "Inference benchmark" }).click();
  await expect(page.locator(".recording-cursor")).toBeVisible();
  await expect(page.locator(".bench-grid")).toContainText("tokens");
  await page.getByRole("link", { name: "Browse catalog" }).click();
  await expect(page.getByLabel("Filter contexts")).toBeVisible();
  const listing = await (await page.request.get("/api/contexts")).json();
  const health = await (await page.request.get("/api/health")).json();
  if (listing.length) await expect(page.getByRole("grid")).toBeVisible();
  else await expect(page.getByText("No published contexts yet")).toBeVisible();
  const state = async () => (await page.request.get("/api/judge/state")).json();
  const original = await state();
  expect(original.kind).toBe("user");
  expect(
    (await context.cookies()).find((c) => c.name === "cachet_workspace")
      ?.httpOnly,
  ).toBe(true);
  await page
    .getByRole("link", { name: "Publish context", exact: true })
    .first()
    .click();
  await expect(page.getByLabel("Licensed source")).toContainText("Starlette");
  await page.getByLabel("Licensed source").selectOption("fastapi");
  if (listing.some((row: { slug: string }) => row.slug === "starlette")) {
    await expect(page.getByLabel("Parent context")).not.toHaveValue("");
  } else {
    await expect(
      page.getByRole("button", {
        name:
          health.mode === "replay" ? "Register for replay" : "Publish and warm",
      }),
    ).toBeDisabled();
  }
  await page.getByRole("link", { name: "Judge mode", exact: true }).click();
  await page.getByRole("button", { name: "Start guided demo" }).click();
  await expect(page.getByText("Workspace open")).toBeVisible();
  const isolated = await state();
  expect(isolated.kind).toBe("judge");
  expect(isolated.workspace_id).not.toBe(original.workspace_id);
  await expect(
    page.getByRole("button", { name: /Exercise the controls/ }),
  ).toBeEnabled();
  if (health.mode === "replay") {
    await page.getByRole("button", { name: /Publish the layers/ }).click();
    await expect(
      page.getByRole("heading", { name: "Recorded workload" }),
    ).toBeVisible();
    await expect(
      page.getByText(/Live warming and hit probes are unavailable/),
    ).toBeVisible();
    // The fresh judge must register both licensed layers before proceeding.
    await expect(
      page.getByRole("button", { name: "Continue to authorization" }),
    ).toHaveCount(0);
    await expect(page.getByLabel("PayPal receiver email")).toHaveValue("");
  }
  await page.getByRole("button", { name: /Exercise the controls/ }).click();
  if (health.mode === "replay")
    await expect(
      page.getByRole("checkbox", { name: /Clear the shared cache/ }),
    ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Clear cache (test)" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Exit judge mode" }).click();
  await expect(
    page.getByRole("button", { name: "Start guided demo" }),
  ).toBeVisible();
  expect((await state()).workspace_id).toBe(original.workspace_id);
  await page.getByRole("link", { name: "Settlement", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Prefix Royalty Tree" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Linear", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Linear", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  const statements = page.getByRole("navigation", {
    name: "Settlement statements",
  });
  for (const tab of ["Ledger", "Captures", "Refunds", "Payout batches"]) {
    await statements.getByRole("button", { name: tab }).click();
    await expect(statements.getByRole("button", { name: tab })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(page.locator(".ag-root-wrapper").first()).toBeVisible();
  }
  await page.getByRole("button", { name: /Ops Copilot/ }).click();
  await expect(
    page.getByRole("textbox", { name: "Message input" }),
  ).toBeVisible();
  await expect(
    page.getByText("No PayPal objects reconciled yet"),
  ).toBeVisible();
  await page.goto("/dev/ui");
  await expect(
    page.getByRole("heading", { name: "Money strip" }),
  ).toBeVisible();
  expect(exceptions).toEqual([]);
});

test("protects the gateway and fits mobile navigation", async ({ page }) => {
  const unknown = await page.request.post("/api/admin", { data: {} });
  expect(unknown.status()).toBe(404);
  const forged = await page.request.post("/api/judge/workspace", {
    data: { kind: "judge" },
    headers: { origin: "https://untrusted.invalid" },
  });
  expect(forged.status()).toBe(403);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Toggle navigation" }).click();
  await page.getByRole("link", { name: "Agent console" }).click();
  await expect(
    page.getByRole("heading", { name: "Spending mandate" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.goto("/settlement");
  await expect(
    page.getByRole("heading", { name: "Prefix Royalty Tree" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
