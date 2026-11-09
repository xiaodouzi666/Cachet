import { expect, test } from "@playwright/test";
import manifest from "../package.json";

test("inspects the current manifest through an unlicensed local Studio dashboard", async ({ page }) => {
  const licenseMessages: string[] = [];
  page.on("console", message => {
    if (/license key not found/i.test(message.text())) licenseMessages.push(message.text());
  });
  await page.goto("/dev/studio");
  const widget = page.getByRole("heading", { name: "Source inspector" });
  await expect(widget).toBeVisible();
  await expect(page.locator("details")).toContainText("Declared version");
  const accent = await widget.evaluate(el => getComputedStyle(el).getPropertyValue("--ag-accent-color").trim());
  expect(accent.toLowerCase()).toBe("#0f766e");
  await expect.poll(() => licenseMessages.length).toBeGreaterThan(0);
  await expect(page.locator(".ag-studio-watermark")).toHaveCount(1);
  await expect(page.locator(".ag-studio-watermark")).toBeHidden();

  await page.getByRole("button", { name: "Inspect sources" }).click();
  await expect(page.getByText("Frontend packages has fields Package name, Declared version.")).toBeVisible();
  await page.getByRole("textbox", { name: "Message input" }).fill("What version of next is declared?");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByText(`Declared dependencies: next ${manifest.dependencies.next}.`)).toBeVisible();
});
