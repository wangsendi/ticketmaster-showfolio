"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const os = require("node:os");
const { firefox } = require("playwright");
const { capturePage } = require("../src/capture.cjs");

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
