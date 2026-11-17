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

module.exports = {submitAndWaitForResponse, businessError, passwordFormState, emailAccountIssue, passwordFormIssue, passwordResetVisible};
