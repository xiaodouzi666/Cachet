import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";
import type { ApplicationEvidence, Task } from "../../lib/types";

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
      };
      expect(run.task_id).toBe(task.id);
      expect(run.decision).toBe(decision);
      expect(run.application_check).toBeTruthy();
      const check = run.application_check!;
      outcomes[task.id + "/" + decision] = check.status;
      await expect(primary).toHaveAttribute("data-task", task.id);
      await expect(primary).toHaveAttribute("data-decision", decision);
      const receipt = primary.getByRole("region", {
        name: "Generated application check",
      });
      const final = receipt.locator(".application-result").first();
      await expect(final).toContainText(
        "Final response · Turn " + run.final_turn,
      );
      await expect(final.locator(".tag")).toHaveText(check.status, {
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
        check.status !== "passed"
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
