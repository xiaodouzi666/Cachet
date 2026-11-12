import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";
import type {
  ApplicationEvidence,
  DecisionExplanation,
  Quote,
  Task,
} from "../../lib/types";

test("renders actual archived application outcomes separately from metering", async ({
  page,
  request,
}) => {
  await page.goto("/dev/ui");
  const tasks: Task[] = await (await request.get("/api/tasks")).json();
  const archive = page.locator(".archive-prefix");
  const outcomes: Record<string, string> = {};
  for (const task of tasks) {
    await archive.getByLabel("Recorded coding task").selectOption(task.id);
    for (const decision of ["buy", "compute"]) {
      const primary = archive.locator('.archive-turn[data-alternate="false"]');
      await archive.getByLabel("Recorded strategy").selectOption(decision);
      const response = await request.get("/api/reference-turn", {
        params: { task_id: task.id, decision },
      });
      expect(response.ok()).toBe(true);
      const run = (await response.json()) as ApplicationEvidence & {
        task_id: string;
        decision: string;
        final_turn: number;
        recorded_at: string;
        quote: Quote;
        decision_explanation: DecisionExplanation & { generated_at: string };
      };
      expect(run.task_id).toBe(task.id);
      expect(run.decision).toBe(decision);
      expect(run.application_check).toBeTruthy();
      const check = run.application_check!;
      const displayStatus = check.status === "passed" && check.requested_format_met === false
        ? "failed" : check.status;
      outcomes[task.id + "/" + decision] = check.status;
      await expect(primary).toHaveAttribute("data-task", task.id);
      await expect(primary).toHaveAttribute("data-decision", decision);
      const strategy = primary.getByRole("region", {
        name: "Archived strategy decision",
      });
      expect(run.decision_explanation.source).toBe("recorded");
      expect(run.decision_explanation.reason.trim().length).toBeGreaterThan(0);
      await expect(strategy.getByRole("heading")).toHaveText(
        "Recorded model explanation",
      );
      await expect(primary.locator("p time")).toHaveAttribute(
        "datetime",
        run.recorded_at,
      );
      await expect(strategy).toContainText(
        "Platform-funded · Generated separately for this quote",
      );
      const generatedAt = run.decision_explanation.generated_at;
      expect(generatedAt).toMatch(/(?:Z|\+00:00)$/);
      await expect(strategy.locator("time")).toHaveAttribute(
        "datetime",
        generatedAt,
      );
      await expect(strategy.locator(".tag")).toHaveText(run.quote.decision);
      await expect(strategy.locator("p")).toHaveText(
        run.decision_explanation.reason,
      );
      const dollars = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 6,
      });
      await expect(strategy.locator("strong")).toHaveText([
        dollars.format(run.quote.buy_micros / 1000000),
        dollars.format(run.quote.compute_micros / 1000000),
      ]);
      const receipt = primary.getByRole("region", {
        name: "Generated application check",
      });
      const final = receipt.locator(".application-result").first();
      await expect(final).toContainText(
        "Final response · Turn " + run.final_turn,
      );
      await expect(final.locator(".tag")).toHaveText(displayStatus, {
        ignoreCase: true,
      });
      if (check.reason) await expect(final).toContainText(check.reason);
      const details = final.locator(".check-receipt");
      if ((await details.getAttribute("open")) === null)
        await details.getByText("Inspect verification evidence").click();
      if (check.answer_sha256) {
        await expect(details).toContainText(check.answer_sha256);
        const raw = final
          .locator("details")
          .filter({
            has: page.getByText("Read original final response", {
              exact: true,
            }),
          });
        await raw.locator("summary").click();
        const text = await raw.locator("pre").textContent();
        expect(
          createHash("sha256")
            .update(text ?? "")
            .digest("hex"),
        ).toBe(check.answer_sha256);
      }
      if (check.source_sha256)
        await expect(details).toContainText(check.source_sha256);
      if (check.http != null)
        await expect(details.locator("pre")).toHaveText(
          JSON.stringify(check.http, null, 2),
        );
      if (check.requested_format_met != null)
        await expect(details).toContainText(
          check.requested_format_met ? "Matched" : "Did not match",
        );
      const earlier = receipt.locator(".application-result").nth(1);
      if (
        run.earlier_application_check?.status === "passed" &&
        run.earlier_application_check.requested_format_met !== false &&
        displayStatus !== "passed" &&
        Number(run.verified_application_turn) < run.final_turn
      ) {
        await expect(earlier).toContainText(
          "Earlier module · Turn " + run.verified_application_turn,
        );
        await expect(earlier.locator(".tag")).toHaveText("Passed");
      } else await expect(earlier).toHaveCount(0);
      if (check.status === "failed") {
        const other = decision === "buy" ? "compute" : "buy";
        const otherTrack = await (
          await request.get("/api/reference-turn", {
            params: { task_id: task.id, decision: other },
          })
        ).json();
        const link = archive.getByText(
          `View passing ${other} archive for this task`,
        );
        if (otherTrack.application_check?.status === "passed") {
          await link.click();
          const counterpart = archive.locator(
            '.archive-turn[data-alternate="true"]',
          );
          await expect(counterpart).toHaveAttribute("data-decision", other);
          await expect(
            counterpart.locator(".application-result .tag").first(),
          ).toHaveText("Passed");
          await expect(
            primary.locator(".application-result .tag").first(),
          ).toHaveText("Failed");
        } else await expect(link).toHaveCount(0);
      }
    }
  }
  expect(outcomes).toEqual({
    "rate-limit/buy": "passed",
    "rate-limit/compute": "passed",
    "route-class/buy": "passed",
    "route-class/compute": "passed",
  });
});
