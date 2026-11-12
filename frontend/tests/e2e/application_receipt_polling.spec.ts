import { expect, test, type Page, type Route } from "@playwright/test";
import type { ApplicationCheck } from "../../lib/types";

type Reply = { status?: number; body: unknown };
type Fixture = {
  receipt: (id: string) => Reply | Promise<Reply>;
  events?: (id: string) => Promise<void>;
  savedCheck?: ApplicationCheck;
};

async function mountRun(page: Page, fixture: Fixture) {
  let current = "run-a";
  await page.route("**/api/**", async (route: Route) => {
    const path = new URL(route.request().url()).pathname.slice(5);
    const respond = (body: unknown, status = 200) => route.fulfill({ json: body, status });
    if (path === "health") return respond({ mode: "live", gpu: "Test fixture" });
    if (path === "judge/workspace") return respond({});
    if (path === "judge/state")
      return respond({
        workspace_id: "test-workspace", kind: "user",
        mandate: { id: "test-mandate", status: "ACTIVE", cap_micros: 1000000, captured_micros: 0 },
        run: { id: current, status: "completed", application_check: fixture.savedCheck || { status: "unverified" } },
      });
    if (path === "contexts")
      return respond([{ id: "context", title: "Starlette", slug: "starlette", publisher_name: "Test", status: "hot" }]);
    if (path === "tasks")
      return respond([{ id: "route-class", title: "Custom route", source_slug: "starlette", turns: 10 }]);
    if (path === "agent/runs" && route.request().method() === "POST") {
      current = "run-b";
      return respond({ run_id: current, status: "completed" });
    }
    const receipt = path.match(/^agent\/runs\/([^/]+)\/application-check$/);
    if (receipt) {
      const reply = await fixture.receipt(receipt[1]);
      return respond(reply.body, reply.status);
    }
    const events = path.match(/^agent\/runs\/([^/]+)\/events$/);
    if (events) {
      await fixture.events?.(events[1]);
      const body = [
        "id: 1", "event: turn",
        'data: {"id":"test-call","turn":1,"mode":"live","prompt_tokens":20,"cached_tokens":10,"ttft_ms":10,"charge_micros":1,"text":"test response"}',
        "", "id: 2", "event: done",
        'data: {"status":"completed","application_check":{"status":"unverified","reason":"Immutable event before check"}}', "", "",
      ].join("\n");
      return route.fulfill({ contentType: "text/event-stream", body });
    }
    return respond({});
  });
  await page.goto("/agent");
  await expect(page.getByTestId("agent-run")).toHaveAttribute("data-run-id", "run-a");
  return page.getByRole("region", { name: "Generated application check" });
}
function receipt(run: string, status: ApplicationCheck["status"], extra = {}) {
  return { body: {
    run_id: run,
    application_check: { status, ...extra },
    verified_application_turn: status === "passed" ? 1 : null,
  }};
}

test("polls queued and checking to a final receipt with verification provenance", async ({ page }) => {
  let calls = 0;
  const check = await mountRun(page, { receipt: (id) => {
    calls += 1;
    return receipt(id, calls === 1 ? "queued" : calls === 2 ? "checking" : "passed", calls < 3 ? {} : {
      requested_format_met: true, answer_sha256: "verified-answer",
      source_sha256: "verified-module", http: { status_codes: [200, 404, 200] },
      checked_at: "2026-11-12T13:00:00Z", checker_version: "isolated-http-v2",
    });
  }});
  await expect(check.locator(".tag")).toHaveText("Queued");
  await expect(check.locator(".tag")).toHaveText("Checking");
  await expect(check.locator(".tag")).toHaveText("Passed");
  await check.getByText("Inspect verification evidence").click();
  await expect(check).toContainText("verified-answer");
  await expect(check).toContainText("isolated-http-v2");
  await expect(check.locator("time")).toHaveAttribute("datetime", "2026-11-12T13:00:00Z");
  expect(calls).toBe(3);
});

test("a late immutable done event cannot replace a completed HTTP receipt", async ({ page }) => {
  let release = () => {};
  const stream = new Promise<void>((resolve) => { release = resolve; });
  const check = await mountRun(page, {
    receipt: (id) => receipt(id, "passed", { requested_format_met: true }),
    events: () => stream,
  });
  await expect(check.locator(".tag")).toHaveText("Passed");
  release();
  await expect(check).toContainText("Final response · Turn 1");
  await expect(check.locator(".tag")).toHaveText("Passed");
  await expect(check).not.toContainText("Immutable event before check");
});

test("a failed refresh keeps the known receipt and can retry without repeating generation", async ({ page }) => {
  let calls = 0;
  const check = await mountRun(page, { receipt: (id) => {
    calls += 1;
    if (calls === 1) return receipt(id, "checking", { answer_sha256: "keep-this-answer" });
    if (calls === 2) return { status: 503, body: { detail: "Checker temporarily unavailable" } };
    return receipt(id, "passed", { requested_format_met: true, answer_sha256: "keep-this-answer" });
  }});
  await expect(check.locator(".tag")).toHaveText("Checking");
  await expect(page.getByText(/Application verification is temporarily unavailable/)).toBeVisible();
  await expect(check.locator(".tag")).toHaveText("Checking");
  await check.getByText("Inspect verification evidence").click();
  await expect(check).toContainText("keep-this-answer");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(check.locator(".tag")).toHaveText("Passed");
  await expect(page.getByTestId("metered-turn")).toHaveCount(1);
});

test("a mismatched run receipt is rejected and a format failure never gets a green pass", async ({ page }) => {
  let calls = 0;
  const check = await mountRun(page, { receipt: (id) => {
    calls += 1;
    return calls === 1
      ? receipt("unrelated-run", "passed", { answer_sha256: "must-not-render" })
      : receipt(id, "passed", { requested_format_met: false, answer_sha256: "format-failure" });
  }});
  await expect(page.getByText(/unrelated verification state/)).toBeVisible();
  await expect(check.locator(".tag")).toHaveText("Unverified");
  await expect(check).not.toContainText("must-not-render");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(check.locator(".tag")).toHaveText("Failed");
  await expect(check.locator(".tag")).not.toHaveClass(/good/);
  await expect(check).toContainText("HTTP checks passed, but the response did not match the requested format.");
});

test("switching runs clears the old receipt and ignores its delayed response", async ({ page }) => {
  let release = () => {};
  let requested = false;
  const oldResult = new Promise<void>((resolve) => { release = resolve; });
  const check = await mountRun(page, { receipt: async (id) => {
    if (id === "run-a") {
      requested = true;
      await oldResult;
      return receipt(id, "passed", { requested_format_met: true, answer_sha256: "old-answer" });
    }
    return receipt(id, "failed", { reason: "New module failed HTTP check", answer_sha256: "new-answer" });
  }});
  await expect.poll(() => requested).toBe(true);
  await page.getByRole("button", { name: "Run agent", exact: true }).click();
  await expect(page.getByTestId("agent-run")).toHaveAttribute("data-run-id", "run-b");
  await expect(check.locator(".tag")).toHaveText("Failed");
  release();
  await check.getByText("Inspect verification evidence").click();
  await expect(check).toContainText("new-answer");
  await expect(check).not.toContainText("old-answer");
});

test("an older unverified run stops polling without inventing an execution result", async ({ page }) => {
  let calls = 0;
  const check = await mountRun(page, { receipt: (id) => {
    calls += 1;
    return receipt(id, "unverified", { reason: "No checker receipt for this older run." });
  }});
  await expect(check).toContainText("No checker receipt for this older run.");
  await page.clock.install();
  await page.clock.fastForward(10000);
  expect(calls).toBe(1);
  await expect(check.locator(".tag")).toHaveText("Unverified");
});

test("a saved receipt survives an older done event while the status refresh is pending", async ({ page }) => {
  let release = () => {};
  const statusResponse = new Promise<void>((resolve) => { release = resolve; });
  const check = await mountRun(page, {
    savedCheck: { status: "passed", requested_format_met: true, answer_sha256: "saved-answer" },
    receipt: async (id) => {
      await statusResponse;
      return receipt(id, "passed", { requested_format_met: true, answer_sha256: "saved-answer" });
    },
  });
  await expect(check).toContainText("Final response · Turn 1");
  await expect(check.locator(".tag")).toHaveText("Passed");
  await check.getByText("Inspect verification evidence").click();
  await expect(check).toContainText("saved-answer");
  release();
});
