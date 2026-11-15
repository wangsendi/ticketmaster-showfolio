'use strict';

const EMAIL_SELECTOR = 'input[type="email"], input[name="username"], input#identification';

async function isBrowsingPaused(page) {
  return page.getByText('Your Browsing Activity Has Been Paused', {exact:true})
    .filter({visible:true}).first().isVisible().catch(() => false);
}

async function enterOfficialLogin(page, {
  clickTimeout = 60000,
  emailTimeout = 240000,
  onStage = () => {},
  checkStopped = () => {},
  diagnostics = {},
} = {}) {
  const started = Date.now();
  Object.assign(diagnostics, {clickTimeoutMs:clickTimeout, emailTimeoutMs:emailTimeout, clickCompleted:false, emailVisible:false});
  try {
    onStage('official_sign_in');
    checkStopped();
    const clickStarted = Date.now();
    try {
      // Once dispatched, readiness belongs to the email field. A stalled load
      // event or unrelated resource must not make a successful click fail.
      await page.getByText('Sign In', {exact:true}).filter({visible:true}).first()
        .click({timeout:clickTimeout, noWaitAfter:true});
      diagnostics.clickCompleted = true;
    } finally {
      diagnostics.clickElapsedMs = Date.now() - clickStarted;
    }
    onStage('wait_email');
    checkStopped();
    const emailStarted = Date.now();
    try {
      await page.locator(EMAIL_SELECTOR).first().waitFor({state:'visible', timeout:emailTimeout});
      diagnostics.emailVisible = true;
    } finally {
      diagnostics.emailWaitElapsedMs = Date.now() - emailStarted;
    }
    checkStopped();
  } finally {
    diagnostics.totalElapsedMs = Date.now() - started;
    try { diagnostics.finalHost = new URL(page.url()).hostname; } catch {}
  }
}

module.exports = {enterOfficialLogin, EMAIL_SELECTOR, isBrowsingPaused};
