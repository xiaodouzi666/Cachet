// Plain CommonJS also runs on Node 18 with "node scripts/approve_buyer.ts".
const path = require("node:path");
const { createRequire } = require("node:module");
const root = path.resolve(__dirname, "..");
const fromFrontend = createRequire(path.join(root, "frontend", "package.json"));
const { chromium } = fromFrontend("playwright");
fromFrontend("@next/env").loadEnvConfig(root);

const sandbox = new Set(["sandbox.paypal.com", "www.sandbox.paypal.com"]);
function isSandbox(url) {
  return url.protocol === "https:" && sandbox.has(url.hostname) && !url.username && !url.password;
}

async function main() {
  const [link, orderId, returnLink] = process.argv.slice(2);
  if (!link || !orderId || !returnLink) throw Error("Usage: node scripts/approve_buyer.ts APPROVAL_URL ORDER_ID RETURN_URL");
  const entry = new URL(link);
  if (!isSandbox(entry) || entry.searchParams.get("token") !== orderId) {
    throw Error("Expected this order's PayPal sandbox approval URL.");
  }
  const email = process.env.JUDGE_BUYER_EMAIL;
  const password = process.env.JUDGE_BUYER_PASSWORD;
  if (!email || !password) throw Error("Configure the sandbox JUDGE_BUYER_EMAIL and JUDGE_BUYER_PASSWORD.");
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ locale: "en-US" });
    await page.goto(entry.href, { waitUntil: "domcontentloaded" });
    await approveSandboxPopup(page, { email, password, returnUrl: returnLink, orderId });
  } finally {
    await browser.close();
  }
}

async function approveSandboxPopup(page, {email, password, returnUrl, orderId}) {
  if (!email || !password) throw Error("Provide the dedicated sandbox buyer credentials.");
  const receipt = returnUrl ? new URL(returnUrl) : null;
  page.setDefaultTimeout(6000);
  let sawSandbox = false;
  const deadline = Date.now() + 150000;
  let lastClick;
  while (Date.now() < deadline) {
    if (page.isClosed()) {
      if (sawSandbox) return;
      throw Error("Popup closed before reaching the sandbox.");
    }
    const current = new URL(page.url());
    if (current.href === "about:blank") {
      await new Promise(resolve => setTimeout(resolve, 250));
      continue;
    }
    if (receipt && sawSandbox && current.origin === receipt.origin && (!orderId || current.searchParams.get("token") === orderId)) {
      console.log("Sandbox checkout returned to the application. Read back the order before authorizing.");
      return;
    }
    if (!isSandbox(current)) throw Error("Checkout left the expected sandbox or return page.");
    sawSandbox = true;
    const frames = page.frames().filter(frame => frame.url() && isSandbox(new URL(frame.url())));
    let acted = false;
    for (const frame of frames) {
      const emailField = frame.locator('input[type="email"], #email').first();
      const passField = frame.locator('input[type="password"]').first();
      const next = frame.getByRole("button", { name: /^(next|continue)$/i }).first();
      const login = frame.getByRole("button", { name: /^(log in|sign in)$/i }).first();
      const approval = frame.getByRole("button", { name: /^(agree (&|and) (continue|pay)|pay now|complete purchase|continue to review|continue to payment|continue to checkout|continue to merchant|authorize|continue)$/i }).first();
      let button;
      let phase;
      if (await passField.isVisible()) {
        if (await emailField.isVisible()) await emailField.fill(email);
        await passField.fill(password);
        button = login;
        phase = "login";
      } else if (await emailField.isVisible()) {
        await emailField.fill(email);
        button = next;
        phase = "email";
      } else if (await approval.isVisible()) {
        button = approval;
        phase = "approval";
      } else if (await login.isVisible()) {
        button = login;
        phase = "select-login";
      }
      if (button && await button.isVisible() && await button.isEnabled()) {
        const fingerprint = [current.href, phase, await button.textContent()].join("|");
        if (fingerprint !== lastClick) {
          // Re-check the live frame origin immediately before submitting credentials.
          if (!isSandbox(new URL(frame.url()))) throw Error("Unexpected checkout frame origin.");
          try {
            await button.click();
          } catch (error) {
            if (page.isClosed() && sawSandbox) return;
            throw error;
          }
          lastClick = fingerprint;
          acted = true;
        }
        break;
      }
    }
    if (!acted) await new Promise(resolve => setTimeout(resolve, 350));
  }
  throw Error("Sandbox checkout did not return. Approve this order manually if PayPal requested verification.");
}

module.exports = { approveSandboxPopup };

if (require.main === module) main().catch(error => {
  // Playwright exception call logs can contain form input. Never emit their raw values.
  const message = error.name === "Error" && !error.message.includes("Call log:") ? error.message : "Sandbox browser interaction failed.";
  console.error(message.replace(/https?:\/\/\S+/gi, "[checkout URL]"));
  process.exitCode = 1;
});
