"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {firefox} = require("playwright");
const {dismissOptionalPasskey} = require("../src/passkey.cjs");

test("Firefox skips either optional Passkey label, ignores hidden duplicates and retries only after the cooldown", async () => {
  const browser = await firefox.launch({headless: true});
  try {
    for (const label of ["Not Right Now", "Not Now"]) {
      const context = await browser.newContext();
      let callbacks = 0, credentialPosts = 0;
      await context.route("**/*", async route => {
        const url = new URL(route.request().url());
        if (route.request().method() === "POST") credentialPosts++;
        if (url.hostname === "identity.ticketmaster.com") {
          callbacks++;
          return route.fulfill({contentType: "text/html", body: "<p>Callback fixture</p>"});
        }
        return route.fulfill({contentType: "text/html", body: `
          <h1>Add a passkey</h1>
          <button hidden>Not Right Now</button>
          <button id="add">Add Passkey</button><button id="skip">${label}</button>
          <script>window.clicks=0;window.added=0;
          document.querySelector('#add').onclick=()=>{added++};
          document.querySelector('#skip').onclick=()=>{if(++clicks===2)location.href='https://identity.ticketmaster.com/exchange'};</script>`});
      });
      const page = await context.newPage();
      await page.goto("https://auth.ticketmaster.com/fixture");
      let clock = 0;
      const state = {attempts: [], completed: false};
      assert.equal(await dismissOptionalPasskey(page, state, {now: () => clock}), true);
      assert.equal(await page.evaluate(() => clicks), 1);
      assert.equal(await page.evaluate(() => added), 0);
      assert.equal(state.completed, false, "a click alone cannot confirm the callback");
      clock = 5000;
      assert.equal(await dismissOptionalPasskey(page, state, {now: () => clock}), false);
      assert.equal(await page.evaluate(() => clicks), 1);
      clock = 10000;
      await dismissOptionalPasskey(page, state, {now: () => clock});
      await page.waitForURL("https://identity.ticketmaster.com/exchange");
      assert.equal(state.attempts.length, 2);
      assert.ok(state.attempts.every(attempt => attempt.clicked));
      assert.equal(callbacks, 1);
      assert.equal(credentialPosts, 0);
      await context.close();
    }
  } finally { await browser.close(); }
});

test("Passkey dismissal caps attempts and stops on disabled buttons, cancellation or another domain", async () => {
  let clicks = 0, tick = 0;
  const button = {isVisible: async () => true, isEnabled: async () => true,
    click: async () => { clicks++; }};
  const page = {url: () => "https://auth.ticketmaster.com/fixture", getByRole: () => ({filter: () => ({first: () => button})})};
  const state = {attempts: [], completed: false};
  for (let i = 0; i < 3; i++) {
    await dismissOptionalPasskey(page, state, {now: () => tick});
    tick += 10000;
  }
  assert.equal(clicks, 2);
  button.isEnabled = async () => false;
  assert.equal(await dismissOptionalPasskey(page, {attempts: []}), false);
  assert.equal(clicks, 2);
  await assert.rejects(dismissOptionalPasskey(page, {attempts: []}, {checkStopped: () => {throw new Error("cancelled");}}), /cancelled/);
  assert.equal(await dismissOptionalPasskey({...page, url: () => "https://other.invalid/"}, {attempts: []}), false);
  assert.equal(clicks, 2);
});
