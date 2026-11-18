"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { firefox } = require("playwright");
const { readSession } = require("../src/session-export.cjs");

test("session reads overlap and keep the complete storage state", async () => {
  let cookieReadStarted = false;
  let releaseCookies;
  const signup = [{name: "fixture", value: "test"}];
  const storageState = {cookies: [{domain: ".ticketmaster.ca"}, {domain: "ticketmaster.ca.evil.invalid"}], origins: [{origin: "https://signup.ticketmaster.com", localStorage: []}]};
  const session = await readSession({
    cookies: url => {
      assert.equal(url, "https://signup.ticketmaster.com/");
      cookieReadStarted = true;
      return new Promise(resolve => { releaseCookies = () => resolve(signup); });
    },
    storageState: async () => {
      assert.equal(cookieReadStarted, true);
      releaseCookies();
      return storageState;
    },
  });
  assert.equal(session.signup, signup);
  assert.equal(session.storageState, storageState);
  assert.equal(session.caCookieCount, 1);
});

test("Firefox session export preserves domain/path cookie filtering and local storage", async () => {
  const browser = await firefox.launch({headless: true});
  try {
    const context = await browser.newContext();
    const cookie = {value: "synthetic", path: "/", secure: true};
    await context.addCookies([
      {...cookie, name: "signup", domain: "signup.ticketmaster.com"},
      {...cookie, name: "auth", domain: "auth.ticketmaster.com"},
      {...cookie, name: "ca", domain: ".ticketmaster.ca"},
      {...cookie, name: "subpath", domain: "signup.ticketmaster.com", path: "/private"},
    ]);
    await context.route("**/*", route => route.fulfill({contentType: "text/html", body: "<p>Local fixture</p>"}));
    const page = await context.newPage();
    await page.goto("https://signup.ticketmaster.com/");
    await page.evaluate(() => localStorage.setItem("fixture", "preserved"));
    const session = await readSession(context);
    assert.deepEqual(session.signup.map(c => c.name), ["signup"]);
    assert.equal(session.storageState.cookies.length, 4);
    assert.equal(session.caCookieCount, 1);
    assert.equal(session.storageState.origins[0].localStorage[0].value, "preserved");
    await context.close();
  } finally { await browser.close(); }
});
