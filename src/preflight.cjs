"use strict";
const { assertBrowserInstalled, configuredBrowser, createBrowserContext } = require("./browser.cjs");

async function main() {
  let browser;
  try {
    const selected = assertBrowserInstalled(configuredBrowser());
    browser = await selected.type.launch({ ...selected.launchOptions, headless: false, timeout: 30000 });
    const context = await createBrowserContext(selected, browser);
    let externalRequests = 0;
    await context.route("**/*", (route) => {
      externalRequests++;
      return route.abort();
    });
    const page = await context.newPage();
    await page.goto(
      'data:text/html,' + encodeURIComponent('<!doctype html><title>Local preflight</title><p id="status">ok</p>'),
      {waitUntil:"domcontentloaded", timeout:10000},
    );
    const localPageWorked =
      (await page.locator("#status").innerText()) === "ok";
    if (!localPageWorked || externalRequests)
      throw new Error("Local preflight failed");
    process.stdout.write(
      JSON.stringify(
        {
          browser: selected.name,
          version: browser.version(),
          headless: false,
          localPageWorked,
          externalRequests,
        },
        null,
        2,
      ) + "\n",
    );
    await context.close();
  } finally {
    if (browser) await browser.close();
  }
}
main().catch(() => {
  process.stderr.write("Browser preflight failed\n");
  process.exitCode = 1;
});
