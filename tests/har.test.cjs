"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { firefox } = require("playwright");
const { loginHarFilter } = require("../src/har.cjs");

test("HAR retains auth, campaign metadata and CA audit requests with strict host boundaries", () => {
  const filter = loginHarFilter("https://signup.ticketmaster.com/campaign.v2");
  for (const url of [
    "https://auth.ticketmaster.com/json/account-status-validation",
    "https://auth.ticketmaster.com/json/sign-in?locale=zh-CN",
    "https://identity.ticketmaster.com/exchange?code=fixture",
    "https://signup.ticketmaster.com/graphql",
    "https://signup.ticketmaster.com/campaign.v2/?_rsc=fixture",
    "https://www.ticketmaster.ca/anything",
  ]) assert.ok(filter.test(url), url);
  for (const url of [
    "https://auth.ticketmaster.com/json/sign-in-extra",
    "https://signup.ticketmaster.com/campaignXv2",
    "https://signup.ticketmaster.com/_next/static/large.js",
    "https://signup.ticketmaster.com/image.png",
    "https://signup.ticketmaster.com.evil.invalid/graphql",
    "https://ticketmaster.ca.evil.invalid/",
  ]) assert.equal(filter.test(url), false, url);
});

test("Firefox still loads excluded assets while recording critical response bodies", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "tm-har-filter-"));
  let browser;
  try {
    browser = await firefox.launch({ headless: true });
    const harPath = path.join(directory, "fixture.har");
    const campaign = "https://signup.ticketmaster.com/campaign";
    const context = await browser.newContext({recordHar: {
      path: harPath, content: "embed", mode: "full", urlFilter: loginHarFilter(campaign),
    }});
    await context.route("**/*", route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/campaign") return route.fulfill({contentType: "text/html",
        body: '<script src="/_next/static/large.js"></script>'});
      if (url.pathname.endsWith("large.js")) return route.fulfill({contentType: "text/javascript",
        body: "window.assetLoaded=true;/*" + "x".repeat(1024 * 1024) + "*/"});
      return route.fulfill({contentType: "application/json", body: '{"data":{"fixture":"preserved"}}'});
    });
    const page = await context.newPage();
    await page.goto(campaign);
    assert.equal(await page.evaluate(() => window.assetLoaded), true);
    await page.evaluate(() => fetch("/graphql").then(r => r.json()));
    await context.close();
    const raw = await fs.readFile(harPath, "utf8");
    const entries = JSON.parse(raw).log.entries;
    assert.equal(entries.length, 2);
    assert.ok(entries.some(e => e.request.url.endsWith("/graphql") && e.response.content.text.includes("preserved")));
    assert.ok(Buffer.byteLength(raw) < 100000, "excluded 1 MB script must not inflate the archive");
  } finally {
    if (browser) await browser.close();
    await fs.rm(directory, {recursive: true, force: true});
  }
});
