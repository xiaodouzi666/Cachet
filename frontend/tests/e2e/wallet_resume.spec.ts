import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { mkdir, writeFile, access, chmod } from "node:fs/promises";
import { join } from "node:path";
import { browserTransport } from "./fault-proxy";

const demo = require("../../../video/record_scenes.ts");
async function read(page: Page, path: string) {
  const res = await page.request.get("/api/" + path);
  expect(res.ok()).toBe(true);
  return res.json();
}
async function privateStorage(context: BrowserContext, path?: string) {
  const storage = await context.storageState();
  if (path) {
    await writeFile(path, JSON.stringify(storage), { mode: 0o600 });
    await chmod(path, 0o600);
  }
  return storage;
}
test("retains one approved wallet mandate and resumes its live SSE run after a real browser disconnect", async ({
  browser,
  baseURL,
}, info) => {
  test.setTimeout(12 * 60_000);
  const buyer = process.env.JUDGE_BUYER_EMAIL,
    password = process.env.JUDGE_BUYER_PASSWORD;
  test.skip(
    !buyer || !password,
    "Supply the sandbox buyer for the wallet approval",
  );
  if (!baseURL) throw new Error("Configure the web origin under test");
  const persistence = process.env.CACHET_B2_BROWSER_STATE;
  const existing =
    persistence &&
    (await access(persistence)
      .then(() => true)
      .catch(() => false));
  const direct = await browser.newContext({
    baseURL,
    locale: "en-US",
    ...(existing ? { storageState: persistence } : {}),
  });
  const raw = await direct.newPage();
  let transport: Awaited<ReturnType<typeof browserTransport>> | undefined;
  let through: BrowserContext | undefined;
  let active: string | undefined;
  const history: {stage: string; at: string}[] = [];
  const diagnostic: Record<string, unknown> = { history };
  let networkSnapshot: (() => unknown) | undefined;
  async function checkpoint(stage: string) {
    if (!process.env.CACHET_B2_EVIDENCE_DIR || !active) return;
    diagnostic.stage = stage;
    diagnostic.updated_at = new Date().toISOString();
    history.push({stage, at: String(diagnostic.updated_at)});
    if (networkSnapshot) diagnostic.stream = networkSnapshot();
    await mkdir(process.env.CACHET_B2_EVIDENCE_DIR, {
      recursive: true,
      mode: 0o700,
    });
    await writeFile(
      join(
        process.env.CACHET_B2_EVIDENCE_DIR,
        "wallet-inflight-" + active + ".json",
      ),
      JSON.stringify(diagnostic, null, 2) + "\n",
      { mode: 0o600 },
    );
  }

  try {
    await raw.goto("/agent");
    await expect(
      raw.getByRole("heading", { name: "Spending mandate" }),
    ).toBeVisible();
    const health = await read(raw, "health");
    expect(health.mode).toBe("live");
    let person = await read(raw, "judge/state");
    expect(person.kind).toBe("user");
    await privateStorage(direct, persistence);
    if (!person.mandate?.authorization_id) {
      await raw.getByLabel("Budget (USD)").fill("1.00");
      await demo.approve(
        raw,
        {
          ...person,
          sandbox_buyer_email: buyer,
          sandbox_buyer_password: password,
        },
        async () => {},
        {
          walletOnly: true,
          expectedWalletCapMicros: 1_000_000,
          onOrder: async ({mandateId, orderId}: {mandateId: string; orderId: string}) => {
            active = mandateId;
            Object.assign(diagnostic, {workspace: person.workspace_id, mandate: mandateId, order: orderId});
            await checkpoint("original_wallet_order_created");
          },
        },
      );
      person = await read(raw, "judge/state");
    }
    expect(person.mandate.payment_method).toBe("wallet");
    expect(person.mandate.cap_micros).toBe(1_000_000);
    expect(person.mandate.status).toBe("ACTIVE");
    active = person.mandate.id;
    Object.assign(diagnostic, {workspace:person.workspace_id, mandate:active, order:person.mandate.paypal_order_id,
      authorization:person.mandate.authorization_id});
    await checkpoint("original_wallet_active");
    const catalogPage = await direct.newPage();
    await catalogPage.goto("/catalog");
    await expect(catalogPage.getByLabel("Filter contexts")).toBeVisible();
    const current = await read(raw, "contexts");
    const publisherTag = "sandbox maintainer " + person.workspace_id.slice(0, 8);
    const managed = new Map<string, any>(
      current.filter((row: any) => row.can_manage).map((row: any) => [row.slug, row]),
    );
    const publishedIds: string[] = [];
    await raw.goto("/publish");
    for (const slug of ["starlette", "fastapi"]) {
      if (managed.get(slug)?.status === "hot") continue;
      const address = process.env["PUBLISHER_" + slug.toUpperCase() + "_EMAIL"];
      if (!address) throw new Error("Set both sandbox publisher recipients.");
      await raw.getByLabel("Licensed source").selectOption(slug);
      await raw.getByLabel("Publisher name").fill(slug + " " + publisherTag);
      await raw.getByLabel("PayPal receiver email").fill(address);
      if (slug === "fastapi") {
        const parent = managed.get("starlette");
        expect(parent?.id).toBeTruthy();
        await raw.getByLabel("Parent context").selectOption(parent.id);
      }
      const result = raw.waitForResponse((response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname === "/api/contexts",
      );
      await raw.getByRole("button", { name: "Publish and warm" }).click();
      const saved = await result;
      expect(saved.ok()).toBe(true);
      const published = await saved.json();
      expect(published.can_manage).toBe(true);
      expect(published.slug).toBe(slug);
      if (slug === "fastapi") expect(published.parent_id).toBe(managed.get("starlette").id);
      await expect.poll(async () => {
        const reading = await read(raw, "contexts/" + published.id);
        managed.set(slug, reading);
        return reading.status === "hot" && reading.last_probe_hit_ratio >= 0.95;
      }, { timeout: 90000 }).toBe(true);
      publishedIds.push(published.id);
    }
    await catalogPage.bringToFront();
    const catalog = await read(raw, "contexts");
    for (const slug of ["starlette", "fastapi"]) {
      const ours = managed.get(slug);
      expect(ours?.can_manage).toBe(true);
      expect(ours.last_probe_hit_ratio).toBeGreaterThanOrEqual(0.95);
      await catalogPage.getByLabel("Filter contexts").fill(ours.publisher_name);
      await expect(catalogPage.getByRole("grid").locator(
        'a[href="/agent?context=' + encodeURIComponent(ours.id) + '"]',
      )).toBeVisible({ timeout: 30000 });
    }
    const catalogProof = {
      before_count: current.length,
      after_count: catalog.length,
      before_owned: current.filter((row: any) => row.can_manage).map((row: any) => row.id),
      published_ids: publishedIds,
      browser_refresh_clicked: false,
      layers: [...managed.values()]
        .filter((c: any) => ["starlette", "fastapi"].includes(c.slug))
        .map((c: any) => ({
          id: c.id,
          parent_id: c.parent_id,
          slug: c.slug,
          status: c.status,
          last_probe_at: c.last_probe_at,
          hit_ratio: c.last_probe_hit_ratio,
        })),
    };
    diagnostic.catalog = catalogProof;
    await checkpoint("original_publications_visible");
    await catalogPage.close();
    const storage = await privateStorage(direct, persistence);
    transport = await browserTransport(new URL(baseURL));
    through = await browser.newContext({
      baseURL,
      locale: "en-US",
      storageState: storage,
      proxy: { server: transport.url, bypass: "<-loopback>" },
    });
    const page = await through.newPage();
    const cdp = await through.newCDPSession(page);
    await cdp.send("Network.enable");
    const urls = new Map<string, string>(),
      headers = new Map<string, string>(),
      firstFrame = new Map<string, number>();
    cdp.on("Network.eventSourceMessageReceived", (e) => {
      if (e.eventId && !firstFrame.has(e.requestId))
        firstFrame.set(e.requestId, Number(e.eventId));
    });
    cdp.on("Network.requestWillBeSent", (e) => {
      urls.set(e.requestId, e.request.url);
      for (const [k, v] of Object.entries(e.request.headers))
        if (k.toLowerCase() === "last-event-id")
          headers.set(e.requestId, String(v));
    });
    cdp.on("Network.requestWillBeSentExtraInfo", (e) => {
      for (const [k, v] of Object.entries(e.headers))
        if (k.toLowerCase() === "last-event-id")
          headers.set(e.requestId, String(v));
    });
    await page.goto("/agent");
    await expect(
      page.getByRole("heading", { name: "Spending mandate" }),
    ).toBeVisible();
    expect(transport.forwarded).toBeGreaterThan(0);
    const before = await read(raw, "judge/state");
    expect(before.mandate.id).toBe(active);
    expect(before.run).toBeNull();
    const selected = managed.get("starlette");
    await page.getByLabel("Context", { exact: true }).selectOption(selected.id);
    await page.getByLabel("Coding task").selectOption("route-class");
    await page.getByRole("button", { name: "Get quote" }).click();
    await expect(page.locator(".quote-box .tag")).toHaveText("buy");
    await checkpoint("request_original_run");
    await page.getByRole("button", { name: "Run agent", exact: true }).click();
    const visible = page.getByTestId("metered-turn");
    await expect(visible.first()).toBeVisible({ timeout: 120000 });
    const rid = await page.getByTestId("agent-run").getAttribute("data-run-id");
    expect(rid).toBeTruthy();
    const prior = await visible.count();
    expect(prior).toBeGreaterThan(0);
    expect(prior).toBeLessThan(10);
    diagnostic.run_id = rid;
    diagnostic.turns_before_disconnect = prior;
    const cutSockets = transport.cut();
    diagnostic.closed_sockets = cutSockets;
    const eventPath = "/api/agent/runs/" + rid + "/events";
    const receivedCursor = () =>
      transport!.cursors.find((r) => r.path === eventPath)?.id ||
      [...headers].find(([id]) => {
        const url = urls.get(id);
        return url && new URL(url).pathname === eventPath;
      })?.[1];
    networkSnapshot = () => ({
      cursor: receivedCursor(),
      frames: [...firstFrame]
        .filter(([id]) => {
          const url = urls.get(id);
          return url && new URL(url).pathname === eventPath;
        })
        .map(([id, first]) => ({ first, cursor: headers.get(id) })),
    });
    await checkpoint("socket_interrupted");
    await expect.poll(receivedCursor, { timeout: 45000 }).toMatch(/^\d+$/);
    await checkpoint("native_reconnect");
    await expect(visible).toHaveCount(10, { timeout: 180000 });
    await expect(page.getByTestId("agent-run")).toHaveAttribute(
      "data-run-status",
      "completed",
    );
    const resumedFirst = () =>
      [...headers]
        .filter(([id]) => {
          const u = urls.get(id);
          return u && new URL(u).pathname === eventPath;
        })
        .map(([id, cursor]) => ({
          cursor: Number(cursor),
          first: firstFrame.get(id),
        }))
        .find((r) => r.first !== undefined && r.first > r.cursor);
    await expect.poll(resumedFirst, { timeout: 10000 }).toBeTruthy();
    const ids = await visible.evaluateAll((rows) =>
      rows.map((r) => r.getAttribute("data-call-id")),
    );
    const numbers = await visible.evaluateAll((rows) =>
      rows.map((r) => Number(r.getAttribute("data-turn"))),
    );
    expect(numbers).toEqual(Array.from({ length: 10 }, (_, i) => i + 1));
    expect(new Set(ids).size).toBe(10);
    const recorded = await raw.request.get(eventPath);
    const events = (await recorded.text()).split(/\r?\n\r?\n/).flatMap((f) => {
      const name = f.match(/^event: (.+)$/m)?.[1],
        data = f.match(/^data: (.+)$/m)?.[1];
      return name && data ? [{ name, data: JSON.parse(data) }] : [];
    });
    const turns = events.filter((e) => e.name === "turn").map((e) => e.data);
    expect(turns.map((t) => t.id)).toEqual(ids);
    expect(
      turns.every(
        (t) => t.mode === "live" && t.charge_micros > 0 && t.ttft_ms > 0,
      ),
    ).toBe(true);
    const end = await read(raw, "judge/state");
    expect(end.workspace_id).toBe(before.workspace_id);
    expect(end.run.id).toBe(rid);
    expect(end.mandate.authorization_id).toBe(before.mandate.authorization_id);
    await checkpoint("request_original_mandate_close");
    await page
      .getByRole("button", { name: "Close mandate", exact: true })
      .click();
    await expect
      .poll(async () =>
        ["CLOSED", "VOIDED"].includes(
          (await read(raw, "judge/state")).mandate.status,
        ),
      )
      .toBe(true);
    const closed = (await read(raw, "judge/state")).mandate;
    diagnostic.closed_status = closed.status;
    await checkpoint("original_mandate_closed");
    const recon = await read(raw, "reconcile");
    expect(recon.ok).toBe(true);
    expect(recon.differences).toEqual([]);
    const packet = {
      origin: new URL(baseURL).origin,
      catalog: catalogProof,
      task_id: "route-class",
      verified_at: new Date().toISOString(),
      workspace: person.workspace_id,
      mandate: active,
      order: person.mandate.paypal_order_id,
      authorization: person.mandate.authorization_id,
      run: rid,
      turns_before_disconnect: prior,
      closed_sockets: cutSockets,
      last_event_id: receivedCursor(),
      first_resumed_sse_id: resumedFirst()?.first,
      turn_ids: ids,
      metered_charge_micros: turns.reduce((v, t) => v + t.charge_micros, 0),
      closed: {
        status: closed.status,
        forgiven_micros: closed.forgiven_micros,
        captured_micros: closed.captured_micros,
        released_micros: closed.released_micros,
      },
      reconciled_objects: recon.checked,
    };
    await info.attach("wallet-resume-receipt.json", {
      body: Buffer.from(JSON.stringify(packet, null, 2)),
      contentType: "application/json",
    });
    if (process.env.CACHET_B2_EVIDENCE_DIR) {
      await mkdir(process.env.CACHET_B2_EVIDENCE_DIR, {
        recursive: true,
        mode: 0o700,
      });
      await writeFile(
        join(
          process.env.CACHET_B2_EVIDENCE_DIR,
          "wallet-resume-" + active + ".json",
        ),
        JSON.stringify(packet, null, 2) + "\n",
        { mode: 0o600, flag: "wx" },
      );
    }
  } finally {
    await checkpoint("browser_final_state");
    if (through) await through.close();
    if (transport) await transport.close();
    await direct.close();
  }
});
