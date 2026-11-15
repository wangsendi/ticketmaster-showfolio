'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const os=require('node:os');
const http=require('node:http');
const {assertBrowserInstalled,configuredBrowser,createBrowserContext}=require('../src/browser.cjs');
const {capturePage}=require('../src/capture.cjs');

test('runtime driver setting overrides the browser passed by an already running service', async()=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'tm-browser-config-'));
  try {
    const config=path.join(directory,'browser.json');
    assert.equal(configuredBrowser('chromium',config),'chromium');
    await fs.writeFile(config,JSON.stringify({browser:'cloakbrowser'}));
    assert.equal(configuredBrowser('chromium',config),'cloakbrowser');
    await fs.writeFile(config,'{"browser":42}');
    assert.throws(()=>configuredBrowser('chromium',config),/配置文件无效/);
  } finally {await fs.rm(directory,{recursive:true,force:true});}
});

test('CloakBrowser adapter records response bodies, cookies and a full-page screenshot', async()=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'tm-cloak-adapter-'));
  const server=http.createServer((request,response)=>{
    if(request.url==='/record'){
      response.setHeader('Content-Type','application/json');
      response.end(JSON.stringify({fixture:'har-body-preserved'}));
    }else{
      response.setHeader('Content-Type','text/html');
      response.setHeader('Set-Cookie','SID=local-fixture; Path=/');
      response.end('<!doctype html><title>Local adapter check</title><body style="min-height:2200px"><h1>Local fixture</h1><script>fetch("/record")</script></body>');
    }
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser,context;
  try {
    const selected=assertBrowserInstalled('cloakbrowser');
    browser=await selected.type.launch({headless:true,timeout:30000});
    const har=path.join(directory,'fixture.har');
    context=await createBrowserContext(selected,browser,{locale:'en-GB',timezoneId:'Europe/London',recordHar:{path:har,content:'embed',mode:'full'}});
    const page=await context.newPage();
    const recorded=page.waitForResponse(response=>response.url().endsWith('/record'));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await recorded;
    const screenshot=await capturePage(page,path.join(directory,'page.png'));
    assert.equal(screenshot.screenshotFullPage,true);
    assert.ok(screenshot.screenshotHeight>=2200);
    assert.ok((await context.cookies()).some(cookie=>cookie.name==='SID'&&cookie.value==='local-fixture'));
    await context.close();context=null;
    const entries=JSON.parse(await fs.readFile(har,'utf8')).log.entries;
    assert.ok(entries.some(entry=>entry.request.url.endsWith('/record')&&entry.response.content.text.includes('har-body-preserved')));
    assert.match(browser.version(),/^146\./);
  } finally {
    if(context)await context.close();
    if(browser)await browser.close();
    await new Promise(resolve=>server.close(resolve));
    await fs.rm(directory,{recursive:true,force:true});
  }
});
