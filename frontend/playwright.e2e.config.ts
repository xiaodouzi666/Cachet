import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 360000,
  expect: { timeout: 30000 },
  workers: 1,
  outputDir: "../logs/browser-tests",
  use: {
    baseURL: process.env.CACHET_WEB_URL || "http://127.0.0.1:3000",
    trace: "off",
    screenshot: "off",
  },
});
