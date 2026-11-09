// Offline browser selector checks. No checkout navigation or payment requests.
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createRequire } = require("node:module");
const fromFrontend = createRequire(require.resolve("../../frontend/package.json"));
const { chromium } = fromFrontend("playwright");
const { nextButton, passwordSubmit, plainPay } = require("../approve_buyer.ts");

test("choose only the intended login and order controls", async () => {
  const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHROMIUM_CHANNEL || undefined});
  try {
    const page = await browser.newPage({offline: true});
    await page.setContent('<form><input type="email"><button id="btnNext">Volgende</button></form>');
    assert.equal(await nextButton(page).getAttribute("id"), "btnNext");
    await page.setContent('<button>Previous</button>');
    assert.equal(await nextButton(page).count(), 0);

    const password = page.locator('input[type="password"]');
    const login = page.getByRole("button", {name: "Inloggen"});
    await page.setContent('<form><input type="password"><button type="submit" id="btnLogin">Inloggen</button></form>');
    assert.equal(await (await passwordSubmit(password, login)).getAttribute("id"), "btnLogin");
    await page.setContent('<form><button id="btnLogin">Inloggen</button></form><form><input type="password"></form>');
    assert.equal(await (await passwordSubmit(password, login)).count(), 0);
    await page.setContent('<form><input type="password"><button type="reset" id="btnLogin">Reset</button></form>');
    assert.equal(await (await passwordSubmit(password, login)).count(), 0);

    const current = new URL("https://www.sandbox.paypal.com/?token=expected");
    const other = new URL("https://www.sandbox.paypal.com/?token=other");
    const prod = new URL("https://www.paypal.com/?token=expected");
    const ambiguous = new URL("https://www.sandbox.paypal.com/?token=expected&token=other");
    const port = new URL("https://www.sandbox.paypal.com:8443/?token=expected");
    await page.setContent('<button aria-label="Order details $5.00">Order details</button><button>Pay</button>');
    const pick = (top, frame, usd) => plainPay(page, top, frame, "expected", usd);
    assert.equal(await (await pick(current, current, "5.00")).textContent(), "Pay");
    for (const [top, frame, usd] of [
      [current, current, "0.50"], [current, current, undefined], [other, other, "5.00"],
      [current, other, "5.00"], [prod, current, "5.00"], [current, prod, "5.00"],
      [ambiguous, current, "5.00"], [port, current, "5.00"],
    ]) assert.equal(await pick(top, frame, usd), null);
    for (const summary of ["Order details €5.00", "Balance $5.00", "Order details $5.001"]) {
      await page.setContent(`<button>${summary}</button><button>Pay</button>`);
      assert.equal(await pick(current, current, "5.00"), null);
    }
    await page.setContent('<button>Order details $5.00</button><button>Pay later</button>');
    assert.equal(await (await pick(current, current, "5.00")).count(), 0);
  } finally {
    await browser.close();
  }
});
