// Plain CommonJS also runs on Node 18 with "node scripts/approve_buyer.ts".
const path = require("node:path");
const { createRequire } = require("node:module");
const root = path.resolve(__dirname, "..");
const fromFrontend = createRequire(path.join(root, "frontend", "package.json"));
const { chromium } = fromFrontend("playwright");
fromFrontend("@next/env").loadEnvConfig(root);

class CheckoutError extends Error {}

const sandbox = new Set(["sandbox.paypal.com", "www.sandbox.paypal.com"]);
function isSandbox(url) {
  return url.protocol === "https:" && sandbox.has(url.hostname) && !url.port && !url.username && !url.password;
}

function sameOrder(url, orderId) {
  const tokens = url.searchParams.getAll("token");
  return Boolean(orderId) && isSandbox(url) && tokens.length === 1 && tokens[0] === orderId;
}

function nextButton(frame) {
  return frame.locator("button#btnNext").or(
    frame.getByRole("button", { name: /^(next|continue|volgende)$/i }),
  ).filter({ visible: true }).first();
}

async function canClick(button) {
  if (!(await button.isVisible())) return false;
  try {
    await button.click({ trial: true, timeout: 600 });
    return true;
  } catch (error) {
    if (error.name === "TimeoutError") return false;
    throw error;
  }
}

async function passwordSubmit(password, fallback) {
  const form = password.locator("xpath=ancestor::form[1]");
  if (!(await form.count())) return fallback;
  return form.locator('button#btnLogin:not([type]), button#btnLogin[type="submit"], input#btnLogin[type="submit"]').or(
    form.getByRole("button", { name: /^(log in|sign in|inloggen)$/i }),
  ).filter({ visible: true }).first();
}

async function plainPay(frame, current, frameUrl, orderId, expectedUsd) {
  if (!sameOrder(current, orderId) || !isSandbox(frameUrl) ||
      (frameUrl.searchParams.has("token") && !sameOrder(frameUrl, orderId)) ||
      !/^\d+\.\d{2}$/.test(expectedUsd)) return null;
  const amount = expectedUsd.replace(".", "\\.");
  const label = new RegExp(`^order details\\s*(?:US\\$|\\$|USD\\s+)\\s*${amount}$`, "i");
  const summary = frame.getByRole("button", { name: label }).or(frame.getByText(label)).filter({ visible: true });
  if (!(await summary.count())) return null;
  return frame.getByRole("button", { name: /^pay$/i }).filter({ visible: true }).first();
}

function lostFrame(error) {
  return /execution context was destroyed|frame was detached|frame has been detached|element was detached from the dom|cannot find context with specified id/i.test(error.message);
}

async function fillCheckoutField(field, value) {
  try {
    await field.fill(value, { timeout: 1200 });
    return true;
  } catch (error) {
    // The email step can disappear while the sandbox switches to its password form.
    // Re-read the page; never submit the old step after its field disappeared.
    if (error.name === "TimeoutError" && !(await field.isVisible())) return false;
    throw error;
  }
}

function browserError(error) {
  // Playwright call logs can include credentials. Only report fixed diagnostic categories.
  if (error instanceof CheckoutError) return error.message;
  const operation = /^(page|locator)\.(goto|click|fill|isVisible|isEnabled|count|textContent):/.exec(error.message)?.[0];
  const network = /net::(ERR_[A-Z_]+)/.exec(error.message)?.[1];
  const reason = network || [
    [/intercepts pointer events/i, "covered control"], [/element is not enabled/i, "disabled control"],
    [/element is not visible/i, "hidden control"], [/element is not stable/i, "unstable control"],
    [/Timeout|timed out/i, "timed out"],
  ].find(([pattern]) => pattern.test(error.message))?.[1] || "interaction failed";
  return `Sandbox ${operation || "browser:"} ${reason}.`;
}

async function main() {
  const [link, orderId, returnLink, expectedUsd] = process.argv.slice(2);
  if (!link || !orderId || !returnLink) throw new CheckoutError("Usage: node scripts/approve_buyer.ts APPROVAL_URL ORDER_ID RETURN_URL [EXPECTED_USD]");
  const entry = new URL(link);
  if (!sameOrder(entry, orderId)) {
    throw new CheckoutError("Expected this order's PayPal sandbox approval URL.");
  }
  const email = process.env.JUDGE_BUYER_EMAIL;
  const password = process.env.JUDGE_BUYER_PASSWORD;
  if (!email || !password) throw new CheckoutError("Configure the sandbox JUDGE_BUYER_EMAIL and JUDGE_BUYER_PASSWORD.");
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL || undefined });
  try {
    const page = await browser.newPage({ locale: "en-US" });
    await page.goto(entry.href, { waitUntil: "domcontentloaded" });
    await approveSandboxPopup(page, { email, password, returnUrl: returnLink, orderId, expectedUsd });
  } finally {
    await browser.close();
  }
}

async function approveSandboxPopup(page, {email, password, returnUrl, orderId, expectedUsd, beforeApproval}) {
  if (!email || !password) throw new CheckoutError("Provide the dedicated sandbox buyer credentials.");
  const receipt = returnUrl ? new URL(returnUrl) : null;
  page.setDefaultTimeout(6000);
  let sawSandbox = false;
  const deadline = Date.now() + 150000;
  let lastClick;
  while (Date.now() < deadline) {
    if (page.isClosed()) {
      if (sawSandbox) return;
      throw new CheckoutError("Popup closed before reaching the sandbox.");
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
    if (!isSandbox(current)) throw new CheckoutError("Checkout left the expected sandbox or return page.");
    sawSandbox = true;
    const frames = page.frames().filter(frame => frame.url() && isSandbox(new URL(frame.url())));
    let acted = false;
    for (const frame of frames) {
      try {
        const emailField = frame.locator('input:is([type="email"], #email):not([readonly]):not([disabled])').filter({visible:true}).first();
        const passField = frame.locator('input[type="password"]:not([readonly]):not([disabled])').filter({visible:true}).first();
        const login = frame.getByRole("button", { name: /^(log in|sign in|inloggen)$/i }).first();
        const approval = frame.getByRole("button", { name: /^(agree (&|and) (continue|pay)|pay now|complete purchase|continue to review|continue to payment|continue to checkout|continue to merchant|authorize|continue)$/i }).first();
        let button;
        let phase;
        const next = nextButton(frame);
        const hasEmail = await emailField.isVisible();
        if (hasEmail && await canClick(next)) {
          if (!(await fillCheckoutField(emailField, email))) continue;
          button = next;
          phase = "email";
        } else if (await passField.isVisible()) {
          if (hasEmail && !(await fillCheckoutField(emailField, email))) continue;
          if (!(await fillCheckoutField(passField, password))) continue;
          button = await passwordSubmit(passField, login);
          phase = "login";
        } else if (await approval.isVisible()) {
          button = approval;
          phase = "approval";
        } else if (await login.isVisible()) {
          button = login;
          phase = "select-login";
        } else {
          button = await plainPay(frame, current, new URL(frame.url()), orderId, expectedUsd);
          phase = "pay";
        }
        if (button && await button.isVisible() && await button.isEnabled()) {
          const fingerprint = [current.href, phase, await button.textContent()].join("|");
          if (fingerprint !== lastClick) {
            // Re-check the live frame origin immediately before submitting credentials.
            if (!isSandbox(new URL(frame.url()))) throw new CheckoutError("Unexpected checkout frame origin.");
            if (phase === "pay") {
              button = await plainPay(frame, new URL(page.url()), new URL(frame.url()), orderId, expectedUsd);
              if (!button || !(await button.isEnabled())) throw new CheckoutError("The sandbox order or displayed amount changed.");
            }
            if (beforeApproval && sameOrder(new URL(page.url()), orderId) &&
                (phase === "pay" || (phase === "approval" &&
                  /^(agree (&|and) (continue|pay)|pay now|complete purchase|authorize)$/i.test((await button.innerText()).trim())))) {
              await beforeApproval(frame, button, phase);
            }
            console.log(`Sandbox browser attempting: ${phase}.`);
            try {
              await button.click();
              console.log(`Sandbox browser action: ${phase}.`);
            } catch (error) {
              if (page.isClosed() && sawSandbox) return;
              // Email and password steps can change while Playwright waits for an actionable button.
              if (error.name === "TimeoutError" && (phase === "email" || phase === "login") &&
                  !(await button.isVisible())) break;
              if (/intercepts pointer events/i.test(error.message)) {
                const category = await button.evaluate(el => {
                  const r = el.getBoundingClientRect();
                  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                  if (!hit) return "outside the viewport";
                  const identity = [hit.id, hit.className, hit.getAttribute("title"), hit.getAttribute("src")].join(" ");
                  if (/challenge|captcha|security.check/i.test(identity)) return "security challenge";
                  if (/cookie|consent/i.test(identity)) return "cookie notice";
                  if (/spinner|loading|overlay/i.test(identity)) return "overlay";
                  return hit.tagName === "IFRAME" ? "embedded frame" : "another page control";
                });
                throw new CheckoutError(`Sandbox ${phase} control covered by ${category}.`);
              }
              throw error;
            }
            lastClick = fingerprint;
            acted = true;
          }
          break;
        }
      } catch (error) {
        if (!lostFrame(error)) throw error;
        // PayPal can navigate between the visibility check and the next locator operation.
        break;
      }
    }
    if (!acted) await new Promise(resolve => setTimeout(resolve, 350));
  }
  throw new CheckoutError("Sandbox checkout did not return. Approve this order manually if PayPal requested verification.");
}

module.exports = { approveSandboxPopup, nextButton, passwordSubmit, plainPay, fillCheckoutField };

if (require.main === module) main().catch(error => {
  console.error(browserError(error));
  process.exitCode = 1;
});
