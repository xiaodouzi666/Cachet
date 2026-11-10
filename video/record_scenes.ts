// Node 18-compatible CommonJS. Run from the repository root using
// CACHET_WEB_URL=... node video/record_scenes.ts [S2,S3,...] [--rehearsal]
const fs = require("node:fs/promises");
const path = require("node:path");
const { createRequire } = require("node:module");
const { performance } = require("node:perf_hooks");
const root = path.resolve(__dirname, "..");
const fromFrontend = createRequire(path.join(root, "frontend", "package.json"));
const { expect, chromium } = fromFrontend("@playwright/test");
const { approveSandboxPopup } = require("../scripts/approve_buyer.ts");

async function readJson(page, endpoint) {
  const response = await page.request.get("/api/" + endpoint);
  if (!response.ok())
    throw new Error(`The ${endpoint} readback failed (${response.status()}).`);
  return response.json();
}
const readState = (page) => readJson(page, "judge/state");
const quietMark = async () => {};

async function publishLayers(
  page,
  state,
  mark = quietMark,
  { requireLive = false } = {},
) {
  const mode = (await readJson(page, "health")).mode;
  if (requireLive && mode !== "live")
    throw Error("The publication scene requires a live GPU probe.");
  const replay = mode === "replay";
  for (const slug of ["starlette", "fastapi"]) {
    if (!state.publisher_receivers?.[slug])
      throw Error(
        `Configure the sandbox payout receiver for ${slug} to publish this walkthrough.`,
      );
    await page.getByLabel("Licensed source").selectOption(slug);
    await page
      .getByRole("button", { name: /Use configured sandbox receiver/ })
      .click();
    if (slug === "fastapi") {
      const parent = (await readJson(page, "contexts")).find(c => c.can_manage && c.slug === "starlette");
      if (!parent) throw Error("This workspace has no published Starlette parent.");
      await page.getByLabel("Parent context").selectOption(parent.id);
      await expect(page.getByLabel("Parent context")).toHaveValue(parent.id);
    }
    await mark(slug + "-source-and-parent");
    await page
      .getByRole("button", {
        name: replay ? "Register for replay" : "Publish and warm",
      })
      .click();
    const probe = page
      .locator("section")
      .filter({
        has: page.getByRole("heading", {
          name: replay ? "Recorded workload" : "Engine probe",
        }),
      })
      .last();
    await expect(probe).toContainText(new RegExp(slug, "i"));
    if (replay) {
      await expect(probe.locator(".tag")).toHaveText("Registered for replay");
      await mark(slug + "-registered-for-replay");
    } else {
      await expect(probe.locator(".tag")).toHaveText("hot", { timeout: 90000 });
      await expect(probe.locator(".probe-result")).toContainText("%");
      await mark(slug + "-hot");
    }
  }
}
async function approve(
  page,
  state,
  mark = quietMark,
  { walletOnly = false } = {},
) {
  if (!walletOnly && state.card_enabled) {
    await page.getByRole("button", { name: "Use sandbox test card" }).click();
  } else {
    if (
      !state.next_public_paypal_client_id ||
      !state.sandbox_buyer_email ||
      !state.sandbox_buyer_password
    )
      throw Error(
        "Configure the PayPal sandbox application and sandbox buyer to exercise wallet approval.",
      );
    const [popup] = await Promise.all([
      page.waitForEvent("popup", { timeout: 45000 }),
      page.getByLabel("Approve with PayPal sandbox").click(),
    ]);
    try {
      if (walletOnly) {
        await popup.waitForURL(
          (url) =>
            url.protocol === "https:" &&
            ["sandbox.paypal.com", "www.sandbox.paypal.com"].includes(
              url.hostname,
            ),
        );
        await popup.waitForLoadState("domcontentloaded");
        await expect.poll(async () => popup.locator("body").innerText(), {
          timeout:45000, message:"Waiting for the real sandbox checkout to render before redaction",
        }).toMatch(/PayPal|Log in|Continue|Approve|支付|登录/i);
        // Capture the untouched sandbox approval surface. All input and embedded frames are masked.
        await mark("sandbox-wallet-open", popup, [
          popup.locator("input, iframe"),
          popup.getByText(state.sandbox_buyer_email, { exact: false }),
        ]);
      }
      let mandate;
      await expect.poll(async () => {
        mandate = (await readState(page)).mandate;
        return Boolean(
          mandate?.payment_method === "wallet" &&
          ["CREATED", "APPROVED"].includes(mandate.status) &&
          mandate.paypal_order_id &&
          Number.isSafeInteger(mandate.cap_micros) &&
          mandate.cap_micros > 0 && mandate.cap_micros % 10000 === 0,
        );
      }, { timeout: 30000, message: "Waiting for the workspace wallet order and its verified USD budget" }).toBe(true);
      await approveSandboxPopup(popup, {
        orderId: mandate.paypal_order_id,
        expectedUsd: (mandate.cap_micros / 1e6).toFixed(2),
        email: state.sandbox_buyer_email,
        password: state.sandbox_buyer_password,
        returnUrl: new URL(page.url()).origin,
      });
    } finally {
      if (!popup.isClosed()) await popup.close();
      await popup.video()?.delete();
    }
  }
  await expect
    .poll(
      async () => Boolean((await readState(page)).mandate?.authorization_id),
      { timeout: 75000 },
    )
    .toBe(true);
  await mark("sandbox-authorized");
}
function recordingContext(contexts, { replay=false, flushedContextId } = {}) {
  return contexts.find(c => c.can_manage && c.slug === "fastapi" &&
    (flushedContextId ? c.id === flushedContextId : replay || c.status === "hot"));
}
async function selectFastAPI(page, flushedContextId) {
  const contexts = await readJson(page, "contexts");
  const replay = (await readJson(page, "health")).mode === "replay";
  const published = recordingContext(contexts,{replay,flushedContextId});
  if (!published)
    throw Error("The required workspace-owned FastAPI source could not be selected.");
  await page.getByLabel("Context", { exact: true }).selectOption(published.id);
}
async function quote(page, mark = quietMark) {
  await selectFastAPI(page);
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname === "/api/quote"),
    page.getByRole("button", { name: "Get quote" }).click(),
  ]);
  expect(response.ok()).toBe(true);
  const measured = await response.json();
  expect(measured.decision).toBe("buy");
  await expect(page.locator(".quote-box .tag")).toHaveText("buy");
  await mark("measured-quote");
  return measured;
}
async function completedTurns(page, mark = quietMark, runId) {
  const feed=page.getByTestId("agent-run");
  if(runId) await expect(feed).toHaveAttribute("data-run-id",runId);
  await expect(feed.getByTestId("metered-turn")).toHaveCount(10, {
    timeout: 180000,
  });
  await expect
    .poll(async () => {
      const run=(await readState(page)).run;
      return (!runId || run?.id === runId) && run?.status;
    }, { timeout: 60000 })
    .toBe("completed");
  await mark("ten-metered-turns");
}
// The SSE endpoint replays the run's persisted events before streaming new ones.
// Return the first admission tagged by this flush, or all turns after completion.
async function readRunTurns(page, runId, flushId = null) {
  return page.evaluate(
    ({ runId, flushId }) =>
      new Promise((resolve, reject) => {
        const source = new EventSource(
          "/api/agent/runs/" + encodeURIComponent(runId) + "/events",
        );
        const turns = new Map();
        const timer = setTimeout(
          () => finish("Timed out waiting for live run evidence."),
          180000,
        );
        function finish(error) {
          clearTimeout(timer);
          source.close();
          if (error) reject(new Error(error));
          else resolve([...turns.values()]);
        }
        source.addEventListener("turn", (event) => {
          const turn = JSON.parse(event.data);
          if (flushId && turn.cache_flush_id !== flushId) return;
          turns.set(turn.id, turn);
          if (flushId) finish();
        });
        source.addEventListener("done", () =>
          finish(
            flushId && "Run finished without a turn admitted after this flush.",
          ),
        );
        source.addEventListener("error", (event) => {
          if ("data" in event || source.readyState === EventSource.CLOSED)
            finish(
              "The run failed or its event stream closed before completion.",
            );
        });
      }),
    { runId, flushId },
  );
}

function isColdAndFree(turn, flushId) {
  if (!flushId || turn.cache_flush_id !== flushId) return false;
  const publishers =
    turn.spans_json?.filter((s) => s.kind === "publisher") || [];
  return (
    turn.mode === "live" &&
    turn.cached_tokens === 0 &&
    turn.public_charge_micros === 0 &&
    publishers.length > 0 &&
    publishers.every((s) => s.hits === 0 && s.royalty_micros === 0)
  );
}
function isFaultedWarmBreach(turn, mode) {
  return (
    turn.mode === mode &&
    turn.fault_injected_ms > 0 &&
    turn.sla_breach === true &&
    turn.public_charge_micros > 0 &&
    turn.spans_json.some((s) => s.kind === "publisher" && s.hits > 0)
  );
}
const scenes = {
  async S1({ page, go, mark, archiveGallery, s5 }) {
    if (archiveGallery) {
      await go("/dev/ui");
      const archive = page.locator('.archive-turn[data-alternate="false"]');
      await expect(archive).toHaveAttribute("data-task", "rate-limit");
      await expect(archive).toHaveAttribute("data-decision", "buy");
      await archive
        .getByRole("button", { name: "Inspect measured segments" })
        .click();
      await expect(
        page.getByRole("dialog", { name: "Measured prefix segments" }),
      ).toBeVisible();
      await mark("gallery-archived-gpu-trace-prefix");
      return;
    }
    await go("/agent");
    await expect(
      page.getByRole("heading", { name: "Run activity" }),
    ).toBeVisible();
    const response=await page.request.get("/api/judge/state");
    if(response.status()!==401 || s5) expect(response.ok()).toBe(true);
    const workspace=response.ok()?await response.json():null;
    const run = workspace?.run;
    if (s5) expect(run?.id).toBe(s5.runId);
    if (run) {
      await completedTurns(page, quietMark, run.id);
      await page.locator(".prefix-stack").scrollIntoViewIfNeeded();
      await page
        .getByRole("button", { name: "Inspect measured segments" })
        .click();
      await mark("workspace-measured-prefix");
      return {workspaceId:workspace.workspace_id,runId:run.id,mode:run.mode};
    } else {
      await page.getByText("Inspect an archived GPU turn").click();
      await expect(page.getByText("Recorded GPU trace")).toBeVisible();
      await page.locator(".archive-turn").scrollIntoViewIfNeeded();
      await page
        .getByRole("button", { name: "Inspect measured segments" })
        .click();
      await mark("archived-gpu-trace-prefix");
    }
  },
  async S2({ page, go, mark }) {
    await go("/");
    await expect(page.locator(".bench-grid")).toContainText("tokens");
    await mark("homepage");
    await page.getByText("Compare measured context lengths").click();
    await page.locator(".bench-detail").scrollIntoViewIfNeeded();
    await expect(
      page.locator(".bench-detail .bench-bar").first(),
    ).toBeVisible();
    await mark("gpu-benchmark");
    const bands=await readJson(page,"benchmarks");
    const measured=bands.reduce((best,band)=>band.prefix_len>best.prefix_len?band:best);
    expect(measured.prefix_len).toBe(26000);
    expect(measured.cold_ttft_ms_p50).toBeGreaterThan(measured.warm_ttft_ms_p50);
    return {band:measured};
  },
  async S3({ page, go, mark, checkpoint }) {
    // Establish a fresh isolated workspace before the camera records the actual publications.
    const opened = await page.request.post("/api/judge/workspace", { data: { kind: "judge" } });
    expect(opened.ok()).toBe(true);
    const workspace = await opened.json();
    expect(workspace.kind).toBe("judge");
    await checkpoint({workspaceId:workspace.workspace_id});
    await go("/publish");
    const state = await readState(page);
    expect(state.workspace_id).toBe(workspace.workspace_id);
    expect((await readJson(page, "contexts")).filter(c => c.can_manage)).toHaveLength(0);
    await publishLayers(page, state, mark, { requireLive: true });
    const contexts = (await readJson(page, "contexts")).filter(c => c.can_manage);
    const base = contexts.find((c) => c.slug === "starlette");
    const child = contexts.find((c) => c.slug === "fastapi");
    expect(base && child && child.parent_id === base.id).toBeTruthy();
    await go("/judge");
    await page.getByRole("button", { name: "Start guided demo" }).click();
    await expect(page.getByTestId("guide-step-1")).toHaveAttribute("data-complete", "true", {timeout: 120000});
    expect((await readState(page)).workspace_id).toBe(workspace.workspace_id);
    return { workspaceId: workspace.workspace_id, starletteId: base.id, fastapiId: child.id,
      minimumProbeRatio:Math.min(base.last_probe_hit_ratio,child.last_probe_hit_ratio) };
  },
  async S4({ page, go, mark, checkpoint, s3 }) {
    await go("/agent");
    const state = await readState(page);
    expect(state.workspace_id).toBe(s3.workspaceId);
    expect(state.kind).toBe("judge");
    expect(state.mandate).toBeFalsy();
    await approve(page, state, mark, { walletOnly: true });
    const mandate = (await readState(page)).mandate;
    expect(mandate.cap_micros).toBe(5000000);
    expect(mandate.payment_method).toBe("wallet");
    await checkpoint({mandateId:mandate.id, orderId:mandate.paypal_order_id, authorizationId:mandate.authorization_id});
    const measured = await quote(page, mark);
    return { quote:measured, mandateId: mandate.id, orderId: mandate.paypal_order_id, authorizationId: mandate.authorization_id };
  },
  async S5({ page, go, mark, s3, s4 }) {
    await go("/judge");
    const state = await readState(page);
    expect(state.workspace_id).toBe(s3.workspaceId);
    expect(state.mandate?.id).toBe(s4.mandateId);
    expect(state.mandate?.status).toBe("ACTIVE");
    expect((await readJson(page, "health")).mode).toBe("live");
    // Sponsor traffic ends before filming so the later global cold test is attributable.
    await expect.poll(async () => (await readJson(page, "judge/walkthrough")).preparation.status,
      {timeout: 15 * 60 * 1000}).toBe("ready");
    await page.getByTestId("guide-step-3").click();
    const [started] = await Promise.all([
      page.waitForResponse(r => new URL(r.url()).pathname === "/api/judge/task" && r.request().method() === "POST"),
      page.getByRole("button", { name: "Run agent task", exact: true }).click(),
    ]);
    expect(started.ok()).toBe(true);
    const {run_id: runId} = await started.json();
    expect(runId).toBeTruthy();
    await go("/agent");
    const decision = page.locator("ul.events > li").filter({has:page.locator("b",{hasText:/^decision$/})});
    await expect(decision).toContainText("Live model explanation", {timeout:120000});
    await expect(decision.locator("span")).toContainText(/buy/i);
    await decision.scrollIntoViewIfNeeded();
    await mark("live-buy-decision");
    const rows = page.getByTestId("agent-run").getByTestId("metered-turn");
    await expect(rows.first()).toBeVisible({timeout: 120000});
    await rows.first().scrollIntoViewIfNeeded();
    await mark("first-metered-turn");
    await expect(rows.nth(3)).toBeVisible({timeout:120000});
    await rows.nth(3).scrollIntoViewIfNeeded();
    await mark("live-call-metering");
    await completedTurns(page, mark, runId);
    const turns = await readRunTurns(page, runId);
    expect(turns).toHaveLength(10);
    expect(turns.every(t => t.mode === "live" && t.prompt_tokens > 0 && t.charge_micros > 0)).toBe(true);
    expect(turns.some(t => t.public_charge_micros > 0)).toBe(true);
    return {workspaceId: state.workspace_id, mandateId: state.mandate.id, runId,
      calls: turns.map(t => t.id)};
  },
  async S6({ page, go, mark, s3, s5 }) {
    await go("/judge");
    const state = await readState(page);
    expect(state.workspace_id).toBe(s5.workspaceId);
    expect(state.mandate?.id).toBe(s5.mandateId);
    expect(state.run?.id).toBe(s5.runId);
    const step = n => page.getByTestId("guide-step-" + n);
    await step(4).click();
    await page.getByRole("button", {name:"Capture now (test)",exact:true}).click();
    await expect(step(4)).toHaveAttribute("data-complete","true",{timeout:120000});
    let flushId;
    for (let attempt = 0; attempt < 3; attempt++) {
      await page.getByText("Additional cache test",{exact:true}).click();
      await page.getByRole("button",{name:"Flush cache (global, rate-limited)"}).click();
      const [response] = await Promise.all([
        page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname === "/api/judge/flush-cache"),
        page.getByRole("button",{name:"Confirm global flush"}).click(),
      ]);
      if (response.status() === 429) {
        const seconds = Number(response.headers()["retry-after"]);
        if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 600) throw Error("Invalid global flush cooldown.");
        await new Promise(resolve => setTimeout(resolve, (seconds + 1) * 1000));
        await go("/judge");
        continue;
      }
      if (!response.ok()) throw Error("Global cache flush HTTP " + response.status());
      flushId = (await response.json()).flush_id;
      break;
    }
    expect(flushId).toBeTruthy();
    await mark("global-cache-flushed");
    await go("/agent");
    // A confirmed global flush marks all contexts cold. Select the earlier verified
    // source by ID so the next live generation owns the first post-flush observation.
    await selectFastAPI(page, s3.fastapiId);
    const [started] = await Promise.all([
      page.waitForResponse(r=>r.request().method()==="POST" && new URL(r.url()).pathname==="/api/agent/runs"),
      page.getByRole("button",{name:"Run agent",exact:true}).click(),
    ]);
    expect(started.ok()).toBe(true);
    const {run_id:coldRun}=await started.json();
    const [cold]=await readRunTurns(page,coldRun,flushId);
    expect(isColdAndFree(cold,flushId)).toBe(true);
    expect(cold.id).toMatch(/^[a-zA-Z0-9_-]+$/);
    const coldRow=page.locator('[data-testid="metered-turn"][data-call-id="'+cold.id+'"]');
    await expect(coldRow).toContainText("Cold · free public prefix");
    await coldRow.scrollIntoViewIfNeeded();
    await mark("cold-free-public-prefix");
    await completedTurns(page, quietMark, coldRun);
    await go("/judge");
    await step(5).click();
    const postTo = route => response => response.request().method()==="POST" && new URL(response.url()).pathname===route;
    const auditObserved = page.waitForResponse(postTo("/api/judge/fault-audit"),{timeout:240000}).catch(()=>null);
    const [faultStart] = await Promise.all([
      page.waitForResponse(postTo("/api/judge/fault-run")),
      page.getByRole("button",{name:"Inject three delays, capture and audit (test)",exact:true}).click(),
    ]);
    expect(faultStart.ok()).toBe(true);
    await mark("three-delays-submitted");
    const auditReply=await auditObserved;
    expect(auditReply?.ok()).toBe(true);
    await mark("live-fault-audit");
    await expect(step(5)).toHaveAttribute("data-complete","true",{timeout:240000});
    const guide=await readJson(page,"judge/walkthrough");
    expect(guide.followup.mandate_id).toBe(s5.mandateId);
    const breaches=await readRunTurns(page,guide.followup.id);
    expect(breaches).toHaveLength(3);
    expect(breaches.every(t=>isFaultedWarmBreach(t,"live"))).toBe(true);
    const receipts=guide.refunds.filter(r=>r.id && r.amount_cents>0);
    expect(breaches.every(t=>receipts.some(r=>r.call_ids.includes(t.id)))).toBe(true);
    await expect(page.getByText(receipts[0].id,{exact:true})).toBeVisible();
    await mark("paypal-fault-refund");
    return {workspaceId:s5.workspaceId,mandateId:s5.mandateId,flushId,coldCall:cold.id,
      faultRun:guide.followup.id,refunds:receipts};
  },
  async S8({ page, baseURL, mark }) {
    const graphic = process.env.CACHET_ARCHITECTURE_URL || "/architecture.svg";
    const url = new URL(graphic, baseURL);
    url.searchParams.set("rec", "1");
    if (url.origin !== new URL(baseURL).origin)
      throw Error(
        "The architecture graphic must be on the configured recording origin.",
      );
    const response = await page.goto(url.href);
    if (!response?.ok()) throw Error("Architecture asset did not load.");
    await page.evaluate(() => document.fonts.ready);
    await mark("architecture-and-apis");
  },
};
const {runRecorder} = require("./record_driver.cjs");

async function main() {
  return runRecorder({scenes, chromium, expect, readState, readJson}, process.argv.slice(2));
}
module.exports = { scenes, main, readState, publishLayers, approve, quote, completedTurns,
  isColdAndFree, isFaultedWarmBreach, recordingContext };
if (require.main === module) main().catch(error => {
  console.error(String(error.message).split("Call log:")[0]);
  process.exitCode = 1;
});
