"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { firefox } = require("playwright");
const { waitForSubmitReady, submitAndWaitForResponse } = require("../src/login-request.cjs");

test("an obstructed login waits without posting, then submits exactly once after the overlay disappears", async () => {
  const browser = await firefox.launch({headless: true});
  try {
    const context = await browser.newContext();
    let posts = 0;
    await context.route("**/*", async route => {
      if (new URL(route.request().url()).pathname === "/json/sign-in") {
        posts++;
        await route.fulfill({contentType: "application/json", body: '{"fixture":true}'});
      } else await route.fulfill({contentType: "text/html", body: `
        <button type="submit" style="position:absolute;top:100px;left:100px">Sign in</button>
        <div id="loading" class="loading-overlay" style="position:fixed;inset:0;z-index:10;background:white">Loading</div>
        <script>window.clicks=0;document.querySelector('button').onclick=()=>{clicks++;fetch('/json/sign-in',{method:'POST'})};</script>`});
    });
    const page = await context.newPage();
    await page.goto("https://auth.ticketmaster.com/fixture");
    const blocked = {};
    await assert.rejects(waitForSubmitReady(page, {timeout: 1000, diagnostics: blocked}), {name: "TimeoutError"});
    assert.equal(blocked.before.covered, true);
    assert.equal(blocked.after.classes, "loading-overlay");
    assert.equal(blocked.ready, false);
    assert.equal(posts, 0);
    assert.equal(await page.evaluate(() => clicks), 0);
    await page.evaluate(() => document.querySelector("#loading").remove());
    const ready = {};
    await waitForSubmitReady(page, {timeout: 15000, diagnostics: ready});
    assert.equal(ready.ready, true);
    assert.equal(posts, 0, "a readiness trial must never submit the credentials");
    const response = await submitAndWaitForResponse(page, "/json/sign-in", () =>
      page.locator('button[type="submit"]').click({timeout: 15000, noWaitAfter: true}));
    assert.equal(response.status(), 200);
    assert.equal(posts, 1);
    assert.equal(await page.evaluate(() => clicks), 1);
    await context.close();
  } finally { await browser.close(); }
});

test("cancellation stops readiness before any browser interaction", async () => {
  const diagnostics = {};
  await assert.rejects(waitForSubmitReady({locator: () => ({
    evaluate: () => assert.fail("cancelled check must not inspect the page"),
    click: () => assert.fail("cancelled check must not click"),
  })}, {diagnostics, checkStopped: () => { throw new Error("cancelled"); }}), /cancelled/);
  assert.equal(diagnostics.ready, false);
});
