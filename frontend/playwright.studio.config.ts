import { defineConfig } from "@playwright/test";

const existingServer = process.env.STUDIO_TEST_BASE_URL;
const baseURL = existingServer || "http://127.0.0.1:3687";
if (!["localhost", "127.0.0.1", "[::1]"].includes(new URL(baseURL).hostname)) {
  throw new Error("The unlicensed Studio test requires a loopback URL.");
}

export default defineConfig({
  testDir: "./tests",
  testMatch: "studio.spec.ts",
  timeout: 120_000,
  expect: { timeout: 45_000 },
  outputDir: "../logs/studio-tests",
  use: {
    baseURL,
    channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL,
    screenshot: "only-on-failure",
  },
  webServer: existingServer ? undefined : {
    command: "npm run dev -- --hostname 127.0.0.1 --port 3687",
    url: baseURL,
    reuseExistingServer: false,
    timeout: 90_000,
  },
});
