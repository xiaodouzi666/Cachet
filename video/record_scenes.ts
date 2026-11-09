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
    if (slug === "fastapi")
      await expect(page.getByLabel("Parent context")).not.toHaveValue("");
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
async function selectFastAPI(page) {
  const contexts = await readJson(page, "contexts");
  const replay = (await readJson(page, "health")).mode === "replay";
  const published = contexts.find(
    (c) => c.slug === "fastapi" && (replay || c.status === "hot"),
  );
  if (!published)
    throw Error("Publish and warm FastAPI before requesting its task quote.");
  await page.getByLabel("Context", { exact: true }).selectOption(published.id);
}
async function quote(page, mark = quietMark) {
  await selectFastAPI(page);
  await page.getByRole("button", { name: "Get quote" }).click();
  await expect(page.locator(".quote-box .tag")).toHaveText("buy");
  await mark("measured-quote");
}
async function control(page, label, resultTitle) {
  const prior = page.getByTestId("judge-result");
  const responseWait = page.waitForResponse(
    (r) =>
      r.request().method() !== "OPTIONS" &&
      r.url().includes("/api/") &&
      r.url().includes(resultTitle),
    { timeout: 90000 },
  );
  await page.getByRole("button", { name: label, exact: true }).click();
  const response = await responseWait;
  if (!response.ok())
    throw Error(`${label} failed with HTTP ${response.status()}.`);
  await expect(prior).toBeVisible();
  return response.json();
}
async function inject(page) {
  const receipt = await control(page, "Fault injection (test)", "/judge/fault");
  expect(receipt.calls).toBe(3);
}
async function completedTurns(page, mark = quietMark) {
  await expect(page.locator(".table-scroller tbody tr")).toHaveCount(10, {
    timeout: 180000,
  });
  await expect
    .poll(async () => (await readState(page)).run?.status, { timeout: 60000 })
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

function checkSceneRun(state, scene, status) {
  if (
    !scene ||
    state.workspace_id !== scene.workspaceId ||
    state.mandate?.id !== scene.mandateId ||
    state.run?.mandate_id !== scene.mandateId ||
    state.run?.id !== scene.runId ||
    state.run.mode !== "live" ||
    state.run.status !== status
  )
    throw Error(`S6 requires the same ${status} live S5 run and mandate.`);
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
function matchesRefund(refund, captures, audit, breaches) {
  const capture = captures.find((r) => r.id === refund.capture_id);
  if (
    !capture ||
    !refund.paypal_refund_id ||
    refund.status !== "COMPLETED" ||
    !(refund.amount_cents > 0)
  )
    return false;
  return (
    refund.reason === "sla" &&
    audit.refunds.some((r) => r.paypal_refund_id === refund.paypal_refund_id) &&
    breaches.some(
      (t) =>
        capture.call_ids_json.includes(t.id) &&
        refund.call_ids_json.includes(t.id),
    )
  );
}
function meteredRow(page, turn) {
  return page.locator(".table-scroller tbody tr").filter({
    has: page.locator("td:first-child", {
      hasText: new RegExp(`^${turn}(?!\\d)`),
    }),
  });
}
async function auditAndClose(page, mark = quietMark, scene = null) {
  const state = await readState(page);
  if (scene) checkSceneRun(state, scene, "completed");
  const original = state.mandate;
  const run = state.run;
  if (
    !original ||
    run?.mandate_id !== original.id ||
    run.status !== "completed" ||
    !["live", "hosted", "replay"].includes(run.mode)
  )
    throw Error("Audit requires this mandate's completed metered run.");
  const breaches =
    scene?.breaches ||
    (await readRunTurns(page, run.id)).filter((turn) =>
      isFaultedWarmBreach(turn, run.mode),
    );
  expect(
    breaches.length,
    "Audit requires a measured faulted warm SLA breach in this run",
  ).toBeGreaterThan(0);
  await control(page, "Capture now (test)", "/judge/capture-now");
  const captured = async () =>
    (await readJson(page, "captures")).filter(
      (r) =>
        r.mandate_id === original.id &&
        r.paypal_capture_id &&
        breaches.some((t) => r.call_ids_json.includes(t.id)),
    );
  let captures = await captured();
  if (!captures.length) {
    // Some sandbox accounts support collection only on the final capture.
    await control(page, "Close mandate", "/close");
    captures = await captured();
  }
  expect(captures.length).toBeGreaterThan(0);
  const audit = await control(page, "Audit SLA (test)", "/judge/audit");
  expect(audit.calls_reviewed).toBeGreaterThan(0);
  const refunds = await readJson(page, "refunds");
  const receipt = refunds.find((r) =>
    matchesRefund(r, captures, audit, breaches),
  );
  expect(
    receipt,
    "Expected a completed PayPal refund for the captured SLA breach",
  ).toBeTruthy();
  await expect(page.getByTestId("judge-result")).toContainText(
    receipt.paypal_refund_id,
  );
  if (scene) await page.getByTestId("judge-result").scrollIntoViewIfNeeded();
  await mark("refund-readback");
  if (!["CLOSED", "VOIDED"].includes((await readState(page)).mandate.status))
    await control(page, "Close mandate", "/close");
  const mandate = (await readState(page)).mandate;
  expect(mandate.id).toBe(original.id);
  expect(["CLOSED", "VOIDED"]).toContain(mandate.status);
  expect(mandate.released_micros).toBeGreaterThan(0);
  if (scene) {
    await page.goto("/agent?rec=1");
    const released = page
      .locator(".money-strip > div")
      .filter({ hasText: "Released" });
    await expect(released.locator(".money-val")).toHaveText(
      (mandate.released_micros / 1000000).toLocaleString("en-US", {
        style: "currency",
        currency: "USD",
      }),
    );
    await released.scrollIntoViewIfNeeded();
  }
  await mark("unused-budget-released");
  return { receipt, released_micros: mandate.released_micros };
}

async function eligiblePublishers(page) {
  const contexts = await readJson(page, "contexts");
  const ledger = await readJson(page, "ledger");
  const owners = new Set(contexts.map((c) => c.publisher_id));
  return [...owners].filter(
    (id) =>
      ledger
        .filter((r) => r.account === "publisher:" + id + ":payable")
        .reduce((net, r) => net + r.credit_micros - r.debit_micros, 0) >= 10000,
  );
}
async function settleAndReconcile(
  page,
  mark = quietMark,
  { requireBoth = false } = {},
) {
  const eligible = await eligiblePublishers(page);
  if (requireBoth && eligible.length < 2)
    throw Error(
      "Two eligible publisher balances are needed for S7. Supply the completed operator seed journal with CACHET_S7_SEED_JOURNAL.",
    );
  const outcome = await control(
    page,
    "Settle royalties (test)",
    "/judge/settle",
  );
  if (outcome.status === "below_minimum" && !requireBoth) {
    expect(eligible.length).toBe(0);
    await mark("settlement-below-minimum");
  } else {
    expect(outcome.paypal_batch_id).toBeTruthy();
    const batches = await readJson(page, "payouts");
    const batch = batches.find(
      (r) => r.paypal_batch_id === outcome.paypal_batch_id,
    );
    expect(batch).toBeTruthy();
    if (requireBoth) {
      await expect
        .poll(
          async () => {
            const rows = await readJson(page, "payouts");
            const current = rows.find(
              (r) => r.paypal_batch_id === batch.paypal_batch_id,
            );
            return new Set(
              current?.items
                .filter((i) => i.status === "SUCCESS" && i.paypal_item_id)
                .map((i) => i.publisher_id),
            ).size;
          },
          { timeout: 120000 },
        )
        .toBeGreaterThanOrEqual(2);
    }
  }
  const reconciliation = await control(
    page,
    "Reconcile with PayPal",
    "/reconcile",
  );
  expect(reconciliation.ok).toBe(true);
  expect(reconciliation.checked).toBeGreaterThan(0);
  await mark("paypal-reconciled");
}

async function applySeedJournal(context, baseURL, file) {
  const resolved = await fs.realpath(file);
  const relative = path.relative(
    await fs.realpath(path.join(root, "logs")),
    resolved,
  );
  if (relative.startsWith("..") || path.isAbsolute(relative))
    throw Error(
      "Choose the private operator journal inside the repository logs directory.",
    );
  const stat = await fs.stat(resolved);
  if (!stat.isFile() || (stat.mode & 0o077) !== 0)
    throw Error("The workspace journal must be readable only by its owner.");
  const journal = JSON.parse(await fs.readFile(resolved, "utf8"));
  if (
    !journal.workspace_cookie ||
    !journal.workspace_id ||
    !journal.seed?.distinct_live_sessions
  )
    throw Error("The selected journal has no completed operator seed.");
  const webOrigin = new URL(baseURL);
  const loopback = (url) =>
    ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  const matchingLocal =
    loopback(webOrigin) && loopback(new URL(journal.origin));
  const matchingDeployment =
    process.env.PUBLIC_WEB_URL &&
    new URL(process.env.PUBLIC_WEB_URL).origin === webOrigin.origin &&
    [process.env.CACHET_API_URL, process.env.PUBLIC_API_URL]
      .filter(Boolean)
      .some((api) => new URL(api).origin === new URL(journal.origin).origin);
  if (!matchingLocal && !matchingDeployment)
    throw Error(
      "The private operator journal and configured deployment origins do not match.",
    );
  await context.addCookies([
    {
      name: "cachet_workspace",
      value: journal.workspace_cookie,
      url: new URL(baseURL).origin,
      httpOnly: true,
      sameSite: "Lax",
      secure: new URL(baseURL).protocol === "https:",
    },
  ]);
  const page = await context.newPage();
  try {
    const state = await readState(page);
    if (
      state.kind !== "judge" ||
      state.workspace_id !== journal.workspace_id ||
      !state.operator_seeded ||
      state.operator_seed_run_count < journal.seed.distinct_live_sessions
    )
      throw Error(
        "The recording server did not verify the operator-seeded workspace in this journal.",
      );
  } finally {
    await page.close();
    await page.video()?.delete();
  }
}

const scenes = {
  async S1({ page, go, mark, archiveGallery }) {
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
    if ((await readState(page)).run) {
      await completedTurns(page);
      await page.locator(".prefix-stack").scrollIntoViewIfNeeded();
      await page
        .getByRole("button", { name: "Inspect measured segments" })
        .click();
      await mark("workspace-measured-prefix");
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
  },
  async S3({ page, go, mark }) {
    await go("/judge");
    await page.getByRole("button", { name: "Start guided demo" }).click();
    await expect(page.getByText("Workspace open")).toBeVisible();
    const state = await readState(page);
    await go("/publish");
    await publishLayers(page, state, mark, { requireLive: true });
  },
  async S4({ page, go, mark }) {
    await go("/agent");
    const state = await readState(page);
    await approve(page, state, mark, { walletOnly: true });
    await quote(page, mark);
  },
  async S5({ page, go, mark }) {
    await go("/agent");
    await quote(page);
    const state = await readState(page);
    if (
      state.kind !== "judge" ||
      state.mandate?.status !== "ACTIVE" ||
      state.run ||
      (await readJson(page, "health")).mode !== "live"
    )
      throw Error(
        "Start S5 with the fresh judge workspace, approved mandate and live GPU.",
      );
    const started = page.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        new URL(r.url()).pathname === "/api/agent/runs",
    );
    await page.getByRole("button", { name: "Run agent", exact: true }).click();
    const response = await started;
    if (!response.ok())
      throw Error(`Starting the S5 run failed with HTTP ${response.status()}.`);
    const { run_id: runId } = await response.json();
    expect(runId).toBeTruthy();
    await expect(page.locator(".events")).toContainText("decision");
    await expect(page.locator(".table-scroller tbody tr").first()).toBeVisible({
      timeout: 90000,
    });
    await mark("first-metered-turn");
    return {
      workspaceId: state.workspace_id,
      mandateId: state.mandate.id,
      runId,
    };
  },
  async S6({ page, go, mark, s5 }) {
    await go("/judge");
    checkSceneRun(await readState(page), s5, "running");
    await page.getByRole("button", { name: /Exercise the controls/ }).click();
    await page
      .getByRole("checkbox", { name: /Clear the shared cache/ })
      .check();
    const { flush_id: flushId } = await control(
      page,
      "Clear cache (test)",
      "/judge/flush-cache",
    );
    expect(
      flushId,
      "The GPU flush must return its admission marker",
    ).toBeTruthy();
    await mark("test-cache-flush");
    const [cold] = await readRunTurns(page, s5.runId, flushId);
    expect(
      isColdAndFree(cold, flushId),
      "First live turn admitted after this flush must have zero public charge and publisher hits/royalties",
    ).toBe(true);
    await page.getByRole("button", { name: /Run a coding task/ }).click();
    await expect(meteredRow(page, cold.turn)).toContainText(
      "Cold · free public prefix",
    );
    await meteredRow(page, cold.turn).scrollIntoViewIfNeeded();
    await mark("cold-free-public-prefix");
    await page.getByRole("button", { name: /Exercise the controls/ }).click();
    if (cold.turn >= 10)
      throw Error("No subsequent turn remains for the fault demonstration.");
    checkSceneRun(await readState(page), s5, "running");
    await inject(page);
    await mark("test-fault-installed");
    await page.getByRole("button", { name: /Run a coding task/ }).click();
    await completedTurns(page, mark);
    const turns = await readRunTurns(page, s5.runId);
    const breaches = turns.filter(
      (t) => t.turn > cold.turn && isFaultedWarmBreach(t, "live"),
    );
    expect(
      breaches.length,
      "Expected a later live, delayed publisher hit with a measured SLA breach",
    ).toBeGreaterThan(0);
    const row = meteredRow(page, breaches[0].turn);
    await expect(row).toContainText("Fault injection (test)");
    await expect(row).toContainText("SLA breach");
    await row.scrollIntoViewIfNeeded();
    await mark("faulted-warm-sla-breach");
    await page.getByRole("button", { name: /Exercise the controls/ }).click();
    const { receipt, released_micros } = await auditAndClose(page, mark, {
      ...s5,
      breaches,
    });
    return {
      ...s5,
      flushId,
      coldCallId: cold.id,
      coldTurn: cold.turn,
      refundedTurns: breaches
        .filter((t) => receipt.call_ids_json.includes(t.id))
        .map((t) => ({ callId: t.id, turn: t.turn })),
      paypalRefundId: receipt.paypal_refund_id,
      releasedMicros: released_micros,
    };
  },
  async S7({ page, go, mark }) {
    await go("/judge");
    await page.getByRole("button", { name: /Settle and reconcile/ }).click();
    await settleAndReconcile(page, mark, { requireBoth: true });
    await go("/settlement");
    const tabs = page.getByRole("navigation", {
      name: "Settlement statements",
    });
    await tabs.getByRole("button", { name: "Payout batches" }).click();
    await expect(page.locator(".ag-root-wrapper").first()).toBeVisible();
    await mark("payout-batches");
    await tabs.getByRole("button", { name: "Royalties" }).click();
    for (const name of ["Linear", "Shapley"]) {
      const basis = page.getByRole("button", { name, exact: true });
      await basis.click();
      await expect(basis).toHaveAttribute("aria-pressed", "true");
      await mark(name.toLowerCase() + "-allocation");
    }
    await page.getByRole("button", { name: /Ops Copilot/ }).click();
    const question =
      "How much did Starlette earn this week, and why more per token than FastAPI?";
    await page
      .getByRole("textbox", { name: "Message input" })
      .pressSequentially(question, { delay: 35 });
    const reply = page.waitForResponse(
      (r) =>
        r.url().endsWith("/api/copilot") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Send message" }).click();
    const response = await reply;
    const result = await response.text();
    if (
      !response.ok() ||
      !result.includes("event: done") ||
      result.includes("event: error")
    )
      throw Error("The Ops Copilot could not ground the royalty explanation.");
    const delta = result
      .split(/\r?\n\r?\n/)
      .find((frame) => frame.startsWith("event: delta"));
    const text = delta && JSON.parse(delta.split("\ndata: ")[1]).text.trim();
    if (!text) throw Error("The Ops Copilot returned no evidence.");
    await expect(page.getByText(text, { exact: false }).last()).toBeVisible();
    await mark("copilot-grounded-answer");
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
async function main() {
  const args = process.argv.slice(2);
  const rehearsal = args.includes("--rehearsal");
  const archiveGallery = args.includes("--archive-gallery");
  if (archiveGallery && !rehearsal)
    throw Error("The public archive gallery is available for rehearsals only.");
  const baseURL = process.env.CACHET_WEB_URL;
  if (!baseURL || (!rehearsal && new URL(baseURL).protocol !== "https:"))
    throw Error(
      "Set CACHET_WEB_URL to the production HTTPS site. For a local preview, pass --rehearsal.",
    );
  const names = (
    args.find((arg) => !arg.startsWith("--")) || "S2,S3,S4,S5,S6,S7,S1,S8"
  ).split(",");
  if (names.some((name) => !scenes[name]))
    throw Error("Choose scene IDs S1 through S8.");
  const directory = path.join(
    __dirname,
    "out",
    rehearsal ? "rehearsals" : "takes",
    new Date().toISOString().replace(/[:.]/g, "-"),
  );
  await fs.mkdir(directory, { recursive: true });
  const timeline = { origin: new URL(baseURL).origin, rehearsal, scenes: {} };
  let state, pendingS5Video;
  const scratchRoot = path.join(root, ".cache", "recordings");
  await fs.mkdir(scratchRoot, { recursive: true, mode: 0o700 });
  const scratch = await fs.mkdtemp(path.join(scratchRoot, "take-"));
  const browser = await chromium.launch({
    slowMo: 60,
    channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL || undefined,
  });
  try {
    for (const [index, name] of names.entries()) {
      const context = await browser.newContext({
        baseURL,
        storageState: state,
        viewport: { width: 1920, height: 1080 },
        recordVideo: { dir: scratch, size: { width: 1920, height: 1080 } },
      });
      const useSeed = name === "S7" && process.env.CACHET_S7_SEED_JOURNAL;
      if (useSeed)
        await applySeedJournal(
          context,
          baseURL,
          process.env.CACHET_S7_SEED_JOURNAL,
        );
      const page = await context.newPage();
      const start = performance.now();
      const entry = { status: "recording", events: [] };
      timeline.scenes[name] = entry;
      try {
        const go = async (pathname) => {
          await page.goto(pathname + "?rec=1");
          await expect(page.locator("html")).toHaveClass(/recording/);
        };
        const mark = async (label, target = page, mask = []) => {
          await target.screenshot({
            mask,
            path: path.join(directory, name + "-" + label + ".png"),
          });
          entry.events.push({
            label,
            ms: Math.round(performance.now() - start),
          });
        };
        const evidence = await scenes[name]({
          page,
          go,
          mark,
          baseURL,
          archiveGallery,
          s5: timeline.scenes.S5?.evidence,
        });
        if (evidence) entry.evidence = evidence;
        if (!useSeed) {
          const capture = await context.storageState();
          const domain = new URL(baseURL).hostname;
          state = {
            cookies: capture.cookies.filter(
              (c) =>
                c.domain === domain &&
                ["cachet_workspace", "cachet_user_workspace"].includes(c.name),
            ),
            origins: [],
          };
        }
        entry.status = "captured";
      } catch (e) {
        entry.status = "incomplete";
        throw e;
      } finally {
        const saveVideo = async () => {
          const video = page.video();
          await context.close();
          if (video) {
            const suffix = entry.status === "captured" ? "" : "-incomplete";
            await video.saveAs(path.join(directory, name + suffix + ".webm"));
            await video.delete();
          }
        };
        // Stop S5's camera immediately. Its WebM can finish encoding while
        // S6 opens the controls on the same running ten-turn workload.
        if (
          name === "S5" &&
          entry.status === "captured" &&
          names[index + 1] === "S6"
        ) {
          pendingS5Video = Promise.allSettled([saveVideo()]);
        } else {
          await saveVideo();
        }
        await fs.writeFile(
          path.join(directory, "timeline.json"),
          JSON.stringify(timeline, null, 2) + "\n",
        );
      }
    }
  } finally {
    const [s5Save] = await (pendingS5Video || []);
    await browser.close();
    await fs.rm(scratch, { force: true, recursive: true });
    console.log(path.relative(root, directory));
    if (s5Save?.status === "rejected") throw s5Save.reason;
  }
}
module.exports = {
  readState,
  publishLayers,
  approve,
  quote,
  inject,
  completedTurns,
  auditAndClose,
  settleAndReconcile,
  isColdAndFree,
  isFaultedWarmBreach,
};
if (require.main === module)
  main().catch((e) => {
    console.error(String(e.message).split("Call log:")[0]);
    process.exitCode = 1;
  });
