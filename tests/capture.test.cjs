"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { firefox } = require("playwright");
const { capturePage, capturePageWithRefresh } = require("../src/capture.cjs");

const screenshotTimeout = () => Object.assign(new Error("screenshot timed out"), { name: "TimeoutError" });

test("retries a screenshot on the same page before refreshing", async () => {
  let captures = 0;
  const result = await capturePageWithRefresh({ reload: async () => assert.fail("unexpected refresh") }, "page.png", () => {}, {
    capture: async () => {
      if (++captures === 1) throw screenshotTimeout();
      return { screenshotFullPage: true };
    },
  });
  assert.equal(captures, 2);
  assert.equal(result.screenshotRefreshes, 0);
});

test("screenshot timeouts refresh the same session up to three times", async () => {
  let captures = 0, reloads = 0, verified = 0;
  const page = { reload: async () => { reloads++; } };
  const result = await capturePageWithRefresh(page, "page.png", () => {}, {
    capture: async (current) => {
      assert.equal(current, page);
      if (++captures <= 7) throw screenshotTimeout();
      return { screenshotFullPage: true };
    },
    afterRefresh: async () => { verified++; },
  });
  assert.equal(result.screenshotRefreshes, 3);
  assert.equal(result.screenshotFullPage, true);
  assert.equal(captures, 8);
  assert.equal(reloads, 3);
  assert.equal(verified, 3);
});

test("screenshot refreshes stop after three failures, including reload timeouts", async () => {
  let reloads = 0, captures = 0;
  await assert.rejects(capturePageWithRefresh({ reload: async () => {
    reloads++;
    throw screenshotTimeout();
  } }, "page.png", () => {}, {
    capture: async () => { captures++; throw screenshotTimeout(); },
  }), { name: "TimeoutError" });
  assert.equal(reloads, 3);
  assert.equal(captures, 2);
});

test("successful screenshots and non-timeout failures do not refresh", async () => {
  const page = { reload: async () => assert.fail("unexpected refresh") };
  assert.equal((await capturePageWithRefresh(page, "page.png", () => {}, {
    capture: async () => ({ screenshotFullPage: true }),
  })).screenshotRefreshes, 0);
  await assert.rejects(capturePageWithRefresh(page, "page.png", () => {}, {
    capture: async () => { throw new Error("browser closed"); },
  }), /browser closed/);
});

test("cancellation and failed account verification stop screenshot refreshes", async () => {
  let cancelled = false, reloads = 0;
  const page = { reload: async () => { reloads++; } };
  await assert.rejects(capturePageWithRefresh(page, "page.png", () => {
    if (cancelled) throw new Error("cancelled");
  }, { capture: async () => { cancelled = true; throw screenshotTimeout(); } }), /cancelled/);
  assert.equal(reloads, 0);
  let captures = 0;
  await assert.rejects(capturePageWithRefresh(page, "page.png", () => {}, {
    capture: async () => { captures++; throw screenshotTimeout(); },
    afterRefresh: async () => { throw new Error("account verification failed"); },
  }), /account verification failed/);
  assert.equal(captures, 2);
  assert.equal(reloads, 1);
});

test("captures the whole page, including content loaded after scrolling", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "tm-capture-test-"));
  const browser = await firefox.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 900, height: 600}});
    await page.setContent('<html><body style="margin:0"><main style="height:2300px">Header</main><footer id="end">Selected show</footer><script>window.addEventListener("scroll",()=>{if(scrollY>1000&&!window.added){window.added=true;document.querySelector("main").style.height="3000px";}})</script></body></html>');
    const result = await capturePage(page, path.join(dir, "page.png"));
    assert.equal(result.screenshotWidth, 900);
    assert.ok(result.screenshotHeight >= 3000);
    assert.equal(result.screenshotHeight, await page.evaluate(() => document.documentElement.scrollHeight));
    assert.equal(await page.evaluate(() => scrollY), 0);
    const image = await fs.readFile(path.join(dir, "page.png"));
    assert.equal(image.readUInt32BE(20), result.screenshotHeight);
    await assert.rejects(capturePage(page, path.join(dir, "cancel.png"), () => { throw new Error("cancelled"); }), /cancelled/);
    await assert.rejects(fs.stat(path.join(dir, "cancel.png")));
  } finally {
    await browser.close();
    await fs.rm(dir, {recursive: true, force: true});
  }
});
