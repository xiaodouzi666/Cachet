import { expect, test } from "@playwright/test";

// These actions also drive the production recording, so both verify the same UI.
const flow = require("../../../video/record_scenes.ts");

test("completes all six judge steps with real sandbox receipts", async ({
  page,
}) => {
  test.setTimeout(600000);
  await page.goto("/judge");
  await page.getByRole("button", { name: "Start guided demo" }).click();
  await expect(page.getByText("Workspace open")).toBeVisible();
  const state = await flow.readState(page);
  expect(state.kind).toBe("judge");
  await page.getByRole("button", { name: "Next step" }).click();
  await flow.publishLayers(page, state);
  await page.getByRole("button", { name: "Continue to authorization" }).click();
  await flow.approve(page, state);
  await page.getByRole("button", { name: "Continue to agent task" }).click();
  // Arm the fault before starting the real run so the three measured delays are attributable.
  await page.getByRole("button", { name: /Exercise the controls/ }).click();
  await flow.inject(page);
  await page.getByRole("button", { name: /Run a coding task/ }).click();
  await flow.quote(page);
  await page.getByRole("button", { name: "Run agent", exact: true }).click();
  await flow.completedTurns(page);
  await page.getByRole("button", { name: "Open test controls" }).click();
  await flow.auditAndClose(page);
  await page.getByRole("button", { name: /Settle and reconcile/ }).click();
  await flow.settleAndReconcile(page);
});
