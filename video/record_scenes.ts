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
      await approveSandboxPopup(popup, {
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
async function auditAndClose(page, mark = quietMark) {
  const original = (await readState(page)).mandate;
  await control(page, "Capture now (test)", "/judge/capture-now");
  const captured = async () =>
    (await readJson(page, "captures")).filter(
      (r) => r.mandate_id === original.id && r.paypal_capture_id,
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
  const ids = new Set(captures.map((r) => r.id));
  expect(
    refunds.some(
      (r) =>
        ids.has(r.capture_id) &&
        r.paypal_refund_id &&
        r.status === "COMPLETED" &&
        r.amount_cents > 0,
    ),
  ).toBe(true);
  await mark("refund-readback");
  if (!["CLOSED", "VOIDED"].includes((await readState(page)).mandate.status))
    await control(page, "Close mandate", "/close");
  const mandate = (await readState(page)).mandate;
  expect(["CLOSED", "VOIDED"]).toContain(mandate.status);
  expect(mandate.released_micros).toBeGreaterThan(0);
  await mark("unused-budget-released");
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
  async S1({ page, go, mark }) {
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
    await page.getByRole("button", { name: "Run agent", exact: true }).click();
    await expect(page.locator(".events")).toContainText("decision");
    await expect(page.locator(".table-scroller tbody tr").first()).toBeVisible({
      timeout: 90000,
    });
    await mark("first-metered-turn");
    // Continue the same live run in S6 while the remaining turns are still executing.
  },
  async S6({ page, go, mark }) {
    await go("/judge");
    await page.getByRole("button", { name: /Exercise the controls/ }).click();
    await page
      .getByRole("checkbox", { name: /Clear the shared cache/ })
      .check();
    await control(page, "Clear cache (test)", "/judge/flush-cache");
    await mark("test-cache-flush");
    await inject(page);
    await mark("test-fault-installed");
    await go("/agent");
    await expect(
      page.getByText("Cold · free public prefix").first(),
    ).toBeVisible({ timeout: 120000 });
    await completedTurns(page, mark);
    await expect(page.locator("tbody")).toContainText("Fault injection (test)");
    await go("/judge");
    await page.getByRole("button", { name: /Exercise the controls/ }).click();
    await auditAndClose(page, mark);
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
  let state;
  const scratchRoot = path.join(root, ".cache", "recordings");
  await fs.mkdir(scratchRoot, { recursive: true, mode: 0o700 });
  const scratch = await fs.mkdtemp(path.join(scratchRoot, "take-"));
  const browser = await chromium.launch({
    slowMo: 60,
    channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL || undefined,
  });
  try {
    for (const name of names) {
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
        await scenes[name]({ page, go, mark, baseURL });
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
        const video = page.video();
        await context.close();
        if (video) {
          await video.saveAs(
            path.join(
              directory,
              name +
                (entry.status === "captured" ? "" : "-incomplete") +
                ".webm",
            ),
          );
          await video.delete();
        }
        await fs.writeFile(
          path.join(directory, "timeline.json"),
          JSON.stringify(timeline, null, 2) + "\n",
        );
      }
    }
  } finally {
    await browser.close();
    await fs.rm(scratch, { force: true, recursive: true });
    console.log(path.relative(root, directory));
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
};
if (require.main === module)
  main().catch((e) => {
    console.error(String(e.message).split("Call log:")[0]);
    process.exitCode = 1;
  });
