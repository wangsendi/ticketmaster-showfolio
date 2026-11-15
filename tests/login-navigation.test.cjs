'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {firefox,chromium} = require('playwright');
const {enterOfficialLogin,isBrowsingPaused} = require('../src/login-navigation.cjs');

test('detects visible browsing pause without matching hidden content or ordinary login', async () => {
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage();
    await page.setContent('<h1>Your Browsing Activity Has Been Paused</h1><p>We have detected unusual behavior on either your network or your browser.</p>');
    assert.equal(await isBrowsingPaused(page),true);
    await page.setContent('<h1 hidden>Your Browsing Activity Has Been Paused</h1><input type="email">');
    assert.equal(await isBrowsingPaused(page),false);
    await page.setContent('<h1>Sign In</h1><input type="email">');
    assert.equal(await isBrowsingPaused(page),false);
  } finally {await browser.close();}
});

for (const [engine, browserType] of Object.entries({firefox,chromium})) {
test(`${engine}: continues when delayed login form appears while navigation is still loading`, async () => {
  const sockets = new Set();
  const server = http.createServer((request, response) => {
    response.setHeader('Content-Type','text/html');
    if (request.url === '/login') {
      // The form appears late while an unrelated resource keeps load pending.
      response.end('<!doctype html><title>Local login</title><body><img src="/slow-resource"><script>setTimeout(()=>{document.body.insertAdjacentHTML("beforeend", \'<input type="email" aria-label="Email">\')},600)</script>');
    } else if (request.url === '/slow-resource') {
      response.writeHead(200, {'Content-Type':'image/png'});
      response.flushHeaders();
    } else {
      response.end('<!doctype html><a href="/login">Sign In</a>');
    }
  });
  server.on('connection', socket => {sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser = await browserType.launch({headless:true});
    const page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    const stages = [], diagnostics = {};
    await enterOfficialLogin(page, {clickTimeout:5000,emailTimeout:8000,onStage:stage=>stages.push(stage),diagnostics});
    assert.deepEqual(stages, ['official_sign_in','wait_email']);
    assert.equal(diagnostics.clickCompleted,true);
    assert.equal(diagnostics.emailVisible,true);
    assert.ok(diagnostics.emailWaitElapsedMs >= 400);
    assert.equal(await page.locator('input[type="email"]').isVisible(),true);
    assert.notEqual(await page.evaluate(()=>document.readyState),'complete');
  } finally {
    if (browser) await browser.close();
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve=>server.close(resolve));
  }
});

}

test('honors stop request before clicking sign in', async () => {
  let clicked = false;
  const page = {getByText(){clicked=true;throw new Error('must not click');},url(){return 'https://example.invalid/';}};
  await assert.rejects(enterOfficialLogin(page,{checkStopped(){throw new Error('cancelled');}}),/cancelled/);
  assert.equal(clicked,false);
});
