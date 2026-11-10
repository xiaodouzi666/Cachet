import { expect, test, type Page } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

async function read(page: Page, path: string) {
  const res = await page.request.get("/api/" + path);
  expect(res.ok(), ["Read", path, res.status()].join(" ")).toBeTruthy();
  return res.json();
}

async function meters(page: Page, rid: string) {
  const res = await page.request.get("/api/agent/runs/" + rid + "/events");
  expect(res.ok()).toBeTruthy();
  const frames = (await res.text()).split(/\r?\n\r?\n/).filter(Boolean);
  return frames.flatMap((frame) => {
    const name = frame.match(/^event: (.+)$/m)?.[1];
    const data = frame.match(/^data: (.+)$/m)?.[1];
    return name && data ? [{ name, payload: JSON.parse(data) }] : [];
  });
}

test("completes six real judge steps and reconciles two funded recipients", async ({
  page,
}, info) => {
  test.setTimeout(20 * 60 * 1000);
  const health = await read(page, "health");
  expect(health.mode).toBe("live");
  expect(health.card_enabled).toBe(true);
  expect(health.paypal_configured).toBe(true);
  await page.goto("/judge");
  const status = (n: number) => page.getByTestId("guide-step-" + n);
  const completed = (n: number, timeout = 120_000) =>
    expect(status(n)).toHaveAttribute("data-complete", "true", { timeout });

  await page.getByRole("button", { name: "Start guided demo" }).click();
  await completed(1);
  const initial = await read(page, "judge/walkthrough");
  expect(initial.contexts.map((c: any) => c.slug).sort()).toEqual([
    "fastapi",
    "starlette",
  ]);
  expect(
    initial.contexts.every((c: any) => c.status === "hot" && c.probe >= 0.95),
  ).toBe(true);

  await status(2).click();
  await page.getByRole("button", { name: "Use sandbox test card" }).click();
  await completed(2);
  const authorized = await read(page, "judge/walkthrough");
  expect(authorized.mandate.authorization_id).toBeTruthy();
  expect(authorized.mandate.cap_micros).toBe(5_000_000);
  const mid = authorized.mandate.id;

  await status(3).click();
  await page.getByLabel("Built-in task").selectOption("rate-limit");
  await expect(
    page.getByText("Expected TTFT", { exact: false }).first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Run agent task", exact: true })
    .click();
  await completed(3, 240_000);
  const task = await read(page, "judge/walkthrough");
  const mainEvents = await meters(page, task.run.id);
  const main = mainEvents
    .filter((e) => e.name === "turn")
    .map((e) => e.payload);
  expect(main).toHaveLength(10);
  expect(
    main.every(
      (t) => t.mode === "live" && t.charge_micros > 0 && t.cached_tokens >= 0,
    ),
  ).toBe(true);
  expect(mainEvents.find((e) => e.name === "decision")?.payload.source).toBe(
    "live",
  );
  expect(task.run.mandate_id).toBe(mid);

  await status(4).click();
  await expect(
    page.getByText("Estimated collection", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Capture now (test)", exact: true })
    .click();
  await completed(4);
  const clipped = await read(page, "judge/walkthrough");
  expect(
    clipped.captures.some(
      (c: any) =>
        c.id &&
        c.amount_cents > 0 &&
        c.net_cents === c.amount_cents - c.fee_cents,
    ),
  ).toBe(true);

  await status(5).click();
  const capturePromise = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      new URL(r.url()).pathname === "/api/judge/fault-capture",
    { timeout: 180_000 },
  );
  await page
    .getByRole("button", {
      name: "Inject three delays, capture and audit (test)",
      exact: true,
    })
    .click();
  const captureResponse = await capturePromise;
  expect(captureResponse.ok()).toBe(true);
  const targeted = await captureResponse.json();
  await completed(5, 180_000);
  const fault = await read(page, "judge/walkthrough");
  const faultEvents = await meters(page, fault.followup.id);
  const three = faultEvents
    .filter((e) => e.name === "turn")
    .map((e) => e.payload);
  expect(fault.followup.mandate_id).toBe(mid);
  expect(fault.followup.id).not.toBe(task.run.id);
  expect(three).toHaveLength(3);
  const cids = three.map((x) => x.id).sort();
  expect(
    three.every(
      (t) =>
        t.mode === "live" &&
        t.fault_injected_ms >= 1200 &&
        t.sla_breach &&
        t.public_charge_micros > 0,
    ),
  ).toBe(true);
  expect(targeted.target_call_ids.sort()).toEqual(cids);
  expect(
    [
      ...new Set(
        targeted.allocations
          .filter((x: any) => x.principal_micros > 0 && x.public_micros > 0)
          .map((x: any) => x.call_id),
      ),
    ].sort(),
  ).toEqual(cids);
  const parentCaptures = new Set(targeted.captures.map((x: any) => x.id));
  const refunds = (await read(page, "refunds")).filter(
    (r: any) =>
      r.status === "COMPLETED" &&
      r.reason === "sla" &&
      parentCaptures.has(r.capture_id),
  );
  for (const id of cids)
    expect(
      refunds.some(
        (r: any) =>
          r.paypal_refund_id &&
          r.amount_cents > 0 &&
          r.call_ids_json.includes(id),
      ),
    ).toBe(true);

  await page.reload();
  await completed(5);
  await status(6).click();
  await page
    .getByRole("button", { name: "Close, settle and reconcile" })
    .click();
  await completed(6, 12 * 60_000);
  await expect(page.getByText("Reconciled", { exact: true })).toBeVisible();
  const done = await read(page, "judge/walkthrough");
  expect(done.steps).toEqual([true, true, true, true, true, true]);
  expect(done.mandate.id).toBe(mid);
  expect(done.mandate.released_micros).toBe(
    done.mandate.cap_micros - done.mandate.captured_micros,
  );
  expect(done.settlement.reconciliation).toMatchObject({
    ok: true,
    differences: [],
  });
  expect(done.settlement.reconciliation.checked).toBeGreaterThan(0);
  expect(done.settlement.items).toHaveLength(2);
  expect(
    new Set(done.settlement.items.map((i: any) => i.publisher_id)).size,
  ).toBe(2);
  expect(
    done.settlement.items.every(
      (i: any) => i.id && i.amount_cents >= 1 && i.status === "SUCCESS",
    ),
  ).toBe(true);
  expect(done.preparation.captured_micros).toBeGreaterThan(0);
  expect(done.preparation.status).toBe("ready");
  await page.reload();
  await completed(6);
  const packet = {
    origin: new URL(page.url()).origin,
    build: health.build,
    verified_at: new Date().toISOString(),
    mandate: mid,
    order: authorized.mandate.paypal_order_id,
    authorization: authorized.mandate.authorization_id,
    main_run: task.run.id,
    main_turn_ids: main.map((t) => t.id),
    followup_run: fault.followup.id,
    fault_calls: cids,
    targeted,
    refunds: refunds.map((r: any) => ({
      id: r.paypal_refund_id,
      cents: r.amount_cents,
      capture: r.capture_id,
      calls: r.call_ids_json,
    })),
    closing: done.mandate,
    sponsor: done.preparation,
    settlement: done.settlement,
  };
  await info.attach("judge-receipts.json", {
    body: Buffer.from(JSON.stringify(packet, null, 2)),
    contentType: "application/json",
  });
  if (process.env.CACHET_B7_EVIDENCE_DIR) {
    const path = process.env.CACHET_B7_EVIDENCE_DIR;
    await mkdir(path, { recursive: true, mode: 0o700 });
    await writeFile(
      join(path, `judge-${mid}.json`),
      JSON.stringify(packet, null, 2) + "\n",
      { mode: 0o600, flag: "wx" },
    );
  }
});
