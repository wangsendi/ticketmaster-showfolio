"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { firefox } = require("playwright");
const { capturePage, capturePageWithRefresh, ensureAccountPageWithRefresh, captureFailurePage } = require("../src/capture.cjs");

const screenshotTimeout = () => Object.assign(new Error("screenshot timed out"), { name: "TimeoutError" });

test("error screenshots fall back to the viewport after a timeout without reloading", async () => {
  const options = [];
  const result = await captureFailurePage({screenshot: async value => {
    options.push(value);
    if (value.fullPage) throw screenshotTimeout();
  }}, "failure.png");
  assert.deepEqual(options.map(value => value.fullPage), [true, false]);
  assert.ok(options.every(value => value.timeout === 10000 && value.animations === "disabled"));
  assert.equal(result.failureScreenshotSaved, true);
  assert.equal(result.failureScreenshotFullPage, false);
  assert.equal(result.failureScreenshotAttempts[0].errorType, "TimeoutError");
});

test("missing error screenshots record timeout or closed-page evidence and respect cancellation", async () => {
  let attempts = 0;
  const timedOut = await captureFailurePage({screenshot: async () => { attempts++; throw screenshotTimeout(); }}, "failure.png");
  assert.equal(attempts, 2);
  assert.equal(timedOut.failureScreenshotSaved, false);
  assert.equal(timedOut.failureScreenshotErrorType, "TimeoutError");
  const closed = await captureFailurePage({isClosed: () => true, screenshot: async () => { throw new Error("Closed for fixture-secret"); }}, "failure.png",
    {redactError: value => value.replace("fixture-secret", "<redacted>")});
  assert.equal(closed.failureScreenshotAttempts.length, 1);
  assert.equal(closed.failureScreenshotErrorType, "PageClosed");
  assert.equal(closed.failureScreenshotErrorMessage, "Closed for <redacted>");
  const cancelled = await captureFailurePage({screenshot: () => assert.fail("cancelled screenshot")}, "failure.png", {isStopped: () => true});
  assert.equal(cancelled.failureScreenshotSkipped, "cancelled");
});

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

test("late account responses can recover through the same session without restarting login", async()=>{
  let reloads=0, verifies=0;
  const page={reload:async()=>{reloads++;}};
  const result=await ensureAccountPageWithRefresh(page,{
    verify:async()=>++verifies===3,
    onRefresh:count=>assert.equal(count,reloads+1),
  });
  assert.equal(result.accountPageRefreshes,2);
  assert.equal(reloads,2);
  assert.equal(verifies,3);
});

test("account recovery caps refreshes and stops on cancellation or wrong account",async()=>{
  let reloads=0;
  const page={reload:async()=>{reloads++;}};
  await assert.rejects(ensureAccountPageWithRefresh(page,{verify:async()=>false}),/after three refreshes/);
  assert.equal(reloads,3);
  reloads=0;
  await assert.rejects(ensureAccountPageWithRefresh(page,{verify:async()=>{throw new Error("wrong account");}}),/wrong account/);
  assert.equal(reloads,0);
  await assert.rejects(ensureAccountPageWithRefresh(page,{verify:async()=>true,checkStopped:()=>{throw new Error("cancelled");}}),/cancelled/);
  assert.equal(reloads,0);
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

test("stable pages load lazy images and finish without a second scroll sweep", async () => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "tm-stable-capture-"));
  const browser = await firefox.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 900, height: 600}});
    await page.setContent('<body style="margin:0"><main style="height:3000px">Header</main><img id="lazy" width="100" height="100"><script>window.visits=[];const original=window.scrollTo.bind(window);window.scrollTo=(x,y)=>{visits.push(y);original(x,y)};window.addEventListener("scroll",()=>{if(scrollY>1500&&!window.loaded){window.loaded=true;document.querySelector("img").src="data:image/svg+xml,"+encodeURIComponent(\'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="red"/></svg>\');}})</script>');
    const result = await capturePage(page, path.join(dir, "page.png"));
    assert.ok(result.screenshotHeight >= 3100);
    assert.equal(await page.evaluate(() => document.querySelector("img").naturalWidth), 100);
    assert.equal(await page.evaluate(() => visits.filter(y => y === 800).length), 1);
    assert.equal(await page.evaluate(() => scrollY), 0);
  } finally {
    await browser.close();
    await fs.rm(dir, {recursive: true, force: true});
  }
});
