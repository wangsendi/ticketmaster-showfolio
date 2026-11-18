'use strict';

async function submitAndWaitForResponse(page, pathname, submit) {
  // Attach both rejection handlers before clicking: closing the page while a
  // click fails must not leave a response waiter as an unhandled rejection.
  const response = page.waitForResponse(r =>
    new URL(r.url()).pathname === pathname && r.request().method() === 'POST',
  {timeout:60000});
  const [received] = await Promise.all([response, Promise.resolve().then(submit)]);
  return received;
}

async function waitForSubmitReady(page, { timeout = 60000, diagnostics = {}, checkStopped = () => {} } = {}) {
  const button = page.locator('button[type="submit"]');
  const started = performance.now();
  diagnostics.timeoutMs = timeout;
  diagnostics.ready = false;
  const surface = () => button.evaluate(element => {
    const box = element.getBoundingClientRect();
    const top = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    // Describe the obstruction without storing form values or page text.
    return { covered: !!top && top !== element && !element.contains(top),
      tag: top?.tagName || null, classes: typeof top?.className === 'string' ? top.className.slice(0, 300) : null,
      role: top?.getAttribute('role') || null, ariaBusy: top?.getAttribute('aria-busy') || null };
  }, undefined, { timeout: 3000 }).catch(() => null);
  try {
    checkStopped();
    diagnostics.before = await surface();
    // Trial performs Playwright's normal visibility, stability and hit testing
    // without clicking or submitting. Start the response waiter only afterward.
    await button.click({ trial: true, timeout });
    checkStopped();
    diagnostics.ready = true;
  } catch (error) {
    diagnostics.errorType = error.name || 'Error';
    try {
      checkStopped();
      diagnostics.after = await surface();
    } catch {}
    throw error;
  } finally {
    diagnostics.elapsedMs = Math.round(performance.now() - started);
  }
}

function businessError(body, redact = value => value) {
  const result = {};
  if (!body || typeof body !== 'object') return result;
  for (const key of ['errorType', 'message', 'responseCode', 'errorCode']) {
    if (typeof body[key] === 'string') result[key] = redact(body[key]).slice(0, 1000);
    else if (typeof body[key] === 'number') result[key] = body[key];
  }
  return result;
}

async function passwordFormState(page) {
  const field = await page.locator('input[type="password"]').evaluate(input => ({
    valueLength: input.value.length,
    minLength: input.minLength,
    valid: input.validity.valid,
    tooShort: input.validity.tooShort,
    patternMismatch: input.validity.patternMismatch,
  }));
  return {...field, submitEnabled: await page.locator('button[type="submit"]').isEnabled()};
}

function emailAccountIssue(body) {
  const message = typeof body?.message === 'string' ? body.message : '';
  if (/\b(?:the\s+)?user\s+is\s+not\s+found\b|\baccount\s+(?:does\s+not\s+exist|not\s+found)\b/i.test(message)) return 'account-not-found';
  if (/password\s+reset\s+(?:is\s+)?required|reset\s+your\s+password/i.test(message)) return 'password-reset-required';
  return null;
}

function passwordFormIssue(form) {
  if (form.submitEnabled !== false) return null;
  if (form.tooShort || form.patternMismatch) return 'password-format-invalid';
  if (form.valueLength === 7) return 'password-submit-disabled';
  return null;
}

async function passwordResetVisible(page) {
  return page.getByText(/Set up a new password and get back/i).first().isVisible().catch(()=>false);
}

module.exports = {submitAndWaitForResponse, waitForSubmitReady, businessError, passwordFormState, emailAccountIssue, passwordFormIssue, passwordResetVisible};
