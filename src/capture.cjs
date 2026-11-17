"use strict";

async function capturePage(page, filename, checkStopped = () => {}) {
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  await page.evaluate(() => document.fonts.ready);
  let previousHeight = 0;
  for (let pass = 0; pass < 3; pass++) {
    const height = await page.evaluate(() => document.documentElement.scrollHeight);
    if (height > 50000) throw new Error("Page exceeds full screenshot height limit");
    for (let y = 0; y < height; y += 800) {
      checkStopped();
      await page.evaluate(y => window.scrollTo(0, y), y);
      await wait(120);
    }
    await page.evaluate(() => Promise.all(Array.from(document.images).map(img =>
      img.complete ? Promise.resolve() : Promise.race([
        new Promise(resolve => { img.addEventListener("load", resolve, { once: true }); img.addEventListener("error", resolve, { once: true }); }),
        new Promise(resolve => setTimeout(resolve, 5000))
      ]))));
    if (height === previousHeight) break;
    previousHeight = height;
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await wait(500);
  checkStopped();
  const png = await page.screenshot({ path: filename, fullPage: true, animations: "disabled", timeout: 30000 });
  return { screenshotFullPage: true, screenshotWidth: png.readUInt32BE(16), screenshotHeight: png.readUInt32BE(20) };
}

async function capturePageWithRefresh(page, filename, checkStopped = () => {}, {
  capture = capturePage, onRefresh = () => {}, afterRefresh = () => {},
} = {}) {
  // Keep the authenticated browser session and retry locally before restarting login.
  for (let refreshes = 0; refreshes <= 3; refreshes++) {
    checkStopped();
    try {
      if (refreshes) {
        await onRefresh(refreshes);
        await page.reload({ waitUntil: "domcontentloaded", timeout: 60000 });
        checkStopped();
        await afterRefresh();
      }
      for (let attempt = 0; attempt < 2; attempt++) {
        checkStopped();
        try {
          const result = await capture(page, filename, checkStopped);
          return { ...result, screenshotRefreshes: refreshes };
        } catch (error) {
          checkStopped();
          if (error.name !== "TimeoutError" || attempt === 1) throw error;
        }
      }
    } catch (error) {
      checkStopped();
      if (error.name !== "TimeoutError" || refreshes === 3) throw error;
    }
  }
}

async function ensureAccountPageWithRefresh(page, {
  verify, onRefresh = () => {}, checkStopped = () => {},
}) {
  for (let refreshes = 0; refreshes <= 3; refreshes++) {
    checkStopped();
    try {
      if (refreshes) {
        await onRefresh(refreshes);
        await page.reload({waitUntil:"domcontentloaded", timeout:60000});
        checkStopped();
      }
      if (await verify()) return {accountPageRefreshes:refreshes};
    } catch (error) {
      checkStopped();
      if (error.name !== "TimeoutError") throw error;
      if (refreshes === 3) throw error;
    }
  }
  throw new Error("Authenticated account page not verified after three refreshes");
}

module.exports = { capturePage, capturePageWithRefresh, ensureAccountPageWithRefresh };
