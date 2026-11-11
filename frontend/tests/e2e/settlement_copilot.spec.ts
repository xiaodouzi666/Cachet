import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";

const stateFile = process.env.CACHET_COPILOT_STATE;
const callId = process.env.CACHET_COPILOT_CALL_ID;
const mandateId = process.env.CACHET_COPILOT_MANDATE_ID;

async function question(page: Page, text: string, tool: string) {
  await page.evaluate(() => { delete (window as Window & { __studioStream?: string }).__studioStream; });
  await page.getByRole("textbox", { name: "Message input" }).fill(text);
  const stream = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/copilot" && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Send message" }).click();
  const response = await stream;
  expect(response.ok()).toBeTruthy();
  await page.waitForFunction(() => typeof (window as Window & { __studioStream?: string }).__studioStream === "string",
    undefined, { timeout: 120_000 });
  const body = await page.evaluate(() => (window as Window & { __studioStream?: string }).__studioStream!);
  expect(body).toContain("event: done");
  expect(body).toContain(tool);
  expect(body).not.toContain("event: error");
  const outDir = process.env.CACHET_COPILOT_EVIDENCE_DIR;
  if (outDir) {
    mkdirSync(outDir, { recursive: true, mode: 0o700 });
    // Record content the Studio received, without request headers, cookies or signed events.
    const records = body.replace(/\r\n/g, "\n").split("\n\n").filter(Boolean)
      .map(block => block.split("\n").filter(line => line.startsWith("event: ") || line.startsWith("data: ")).join("\n"));
    writeFileSync(join(outDir, tool + "-stream.json"), JSON.stringify({ text, records }, null, 2) + "\n", { mode: 0o600 });
  }
  return body;
}

test("answers the judge's three questions through the Studio agent and confirms one scoped sandbox refund", async ({
  page, context, baseURL,
}) => {
  test.skip(!stateFile || process.env.CACHET_COPILOT_CONFIRM !== "1",
    "Set a dedicated sandbox workspace and explicit confirmation for a new dispute.");
  test.setTimeout(420_000);
  const allowedWeb = new URL(process.env.CACHET_WEB_URL!).origin;
  expect(new URL(baseURL!).origin).toBe(allowedWeb);
  const journal = JSON.parse(readFileSync(stateFile!, "utf8"));
  expect(journal.origin).toBe(process.env.CACHET_PUBLIC_API_URL);
  await context.addCookies([{
    name: "cachet_workspace", value: journal.workspace_cookie, url: allowedWeb,
  }]);
  async function read(route: string) {
    const response = await page.request.get("/api/" + route);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }
  const evidenceDir = process.env.CACHET_COPILOT_EVIDENCE_DIR;
  async function captureEvidence(name: string) {
    if (!evidenceDir) return;
    mkdirSync(evidenceDir, { recursive: true, mode: 0o700 });
    await page.screenshot({ path: join(evidenceDir, name + ".png") });
  }
  const state = await read("judge/state");
  expect(state.workspace_id).toBe(journal.workspace_id);
  const mandate = mandateId || state.mandate?.id;
  expect(mandate).toBeTruthy();
  const captures: { id: string; mandate_id: string; paypal_capture_id: string; call_ids_json: string[] }[] =
    await read("captures");
  const before: { paypal_refund_id: string; capture_id: string; reason: string; call_ids_json: string[] }[] =
    await read("refunds");
  const journalPath = stateFile! + ".studio-dispute.json";
  const previous = existsSync(journalPath) ? JSON.parse(readFileSync(journalPath, "utf8")) : null;
  if (previous) {
    expect(previous.workspace).toBe(journal.workspace_id);
    expect(previous.mandate).toBe(mandate);
    if (callId) expect(previous.call).toBe(callId);
  }
  const existingReceipt = previous && before.find(r => r.reason === "dispute" &&
    r.call_ids_json.includes(previous.call) && captures.some(c => c.id === r.capture_id && c.mandate_id === mandate));
  const used = new Set(before.filter(r => r.reason === "dispute").flatMap(r => r.call_ids_json));
  const owned = captures.filter(c => c.mandate_id === mandate);
  const candidates = [...new Set(owned.flatMap(c => c.call_ids_json))].filter(id => !used.has(id) && (!callId || callId === id) && (!previous || previous.call === id));
  if (!existingReceipt) expect(candidates.length, "The selected mandate needs a captured call with an open dispute entitlement").toBeGreaterThan(0);
  const royalties: { publisher_id: string; hit_tokens: number }[] = await read("royalties");
  const publisherIds = [...new Set(royalties.filter(r => r.hit_tokens > 0).map(r => r.publisher_id))];
  expect(publisherIds.length).toBeGreaterThanOrEqual(2);
  await page.addInitScript(() => {
    const fetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const response = await fetch(...args);
      if (new URL(response.url, window.location.href).pathname === "/api/copilot") {
        // Chrome may evict CDP bodies for completed event streams. Observe a copy
        // inside the live page while the Studio consumes the original stream.
        void response.clone().text().then(body => {
          (window as Window & { __studioStream?: string }).__studioStream = body;
        });
      }
      return response;
    };
  });
  await page.goto("/settlement");
  await page.getByRole("button", { name: /Ops Copilot/ }).click();
  const weekly = await question(page, "How much did Starlette earn this week, and why more per token than FastAPI?", "publisher_earnings");
  expect(weekly).toContain("Shapley");
  for (const id of publisherIds) {
    await expect(page.getByText(id, { exact: false }).first()).toBeVisible();
  }
  await captureEvidence("weekly-royalties");
  await question(page, `Where did the money from mandate ${mandate} go?`, "mandate_breakdown");
  await expect(page.getByText(owned[0].paypal_capture_id, { exact: false }).first()).toBeVisible();
  await captureEvidence("mandate-breakdown");
  if (existingReceipt) {
    // Resume an acknowledged confirmation without selecting another charge.
    await page.getByRole("button", { name: "Refunds", exact: true }).click();
    await expect(page.locator(".ag-cell").filter({ hasText: existingReceipt.paypal_refund_id }).first()).toBeVisible();
    expect((await read("reconcile")).ok).toBe(true);
    await captureEvidence("refund-grid-readback");
    return;
  }
  let selected = "";
  for (const candidate of candidates.slice(0, 10)) {
    const stream = await question(page, `Refund call ${candidate}.`, "propose_refund");
    const proposal = stream.split(/\r?\n/).filter(line => line.startsWith("data: "))
      .map(line => JSON.parse(line.slice(6))).find(row => row.proposal_id);
    expect(proposal?.call_id).toBe(candidate);
    await expect(page.getByRole("heading", { name: "Confirm refund" })).toBeVisible();
    if (proposal.estimated_refund_cents > 0) {
      selected = candidate;
      break;
    }
    await page.getByRole("button", { name: "Dismiss" }).click();
  }
  expect(selected, "No remaining whole-cent PayPal entitlement among the prepared calls").toBeTruthy();
  expect(await read("refunds")).toEqual(before);
  await captureEvidence("refund-proposal");
  writeFileSync(journalPath + ".tmp", JSON.stringify({ workspace: journal.workspace_id, mandate, call: selected }), { mode: 0o600 });
  renameSync(journalPath + ".tmp", journalPath);
  const confirmation = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/api/copilot/confirm" && response.request().method() === "POST",
    { timeout: 120_000 },
  );
  await page.getByRole("button", { name: "Confirm sandbox refund" }).click();
  const firstResponse = await confirmation;
  expect(firstResponse.ok()).toBe(true);
  const confirmed = await firstResponse.json();
  expect(confirmed.call_id).toBe(selected);
  if (evidenceDir) {
    const evidenceFile = join(evidenceDir, "first-confirmation.json");
    if (!existsSync(evidenceFile)) {
      writeFileSync(evidenceFile, JSON.stringify(confirmed, null, 2) + "\n", { flag: "wx", mode: 0o600 });
    }
  }
  await expect(page.getByRole("heading", { name: "Confirm refund" })).toHaveCount(0, { timeout: 120_000 });
  const after = await read("refunds");
  const priorIds = new Set(before.map((r: { paypal_refund_id: string }) => r.paypal_refund_id));
  const refunds = after.filter((r: {
    capture_id: string; reason: string; call_ids_json: string[]; status: string; paypal_refund_id: string;
  }) => owned.some(c => c.id === r.capture_id) && r.reason === "dispute" && r.status === "COMPLETED" &&
    r.call_ids_json.includes(selected) && !priorIds.has(r.paypal_refund_id));
  expect(refunds).toHaveLength(1);
  await expect(page.locator("pre.receipt").last()).toContainText(refunds[0].paypal_refund_id);
  await captureEvidence("confirmed-sandbox-refund");
  expect((await read("reconcile")).ok).toBe(true);
});
