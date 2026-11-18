"use strict";

async function dismissOptionalPasskey(page, state, {
  checkStopped = () => {}, onAttempt = () => {}, now = Date.now, retryDelayMs = 10000,
} = {}) {
  checkStopped();
  if (state.completed || state.attempts.length >= 2 || now() < (state.retryNotBefore || 0)) return false;
  if (new URL(page.url()).hostname !== "auth.ticketmaster.com") return false;
  const button = page.getByRole("button", {name: /^Not (?:Right )?Now$/i}).filter({visible: true}).first();
  if (!(await button.isVisible().catch(() => false)) || !(await button.isEnabled({timeout: 1000}).catch(() => false))) return false;
  checkStopped();
  const attempt = {atUTC: new Date().toISOString(), clicked: false};
  state.attempts.push(attempt);
  onAttempt(state.attempts.length);
  const started = performance.now();
  try {
    await button.click({timeout: 15000, noWaitAfter: true});
    attempt.clicked = true;
  } catch (error) {
    attempt.errorType = error.name || "Error";
    if (error.name !== "TimeoutError") throw error;
  } finally {
    attempt.elapsedMs = Math.round(performance.now() - started);
    state.retryNotBefore = now() + retryDelayMs;
  }
  return true;
}

module.exports = {dismissOptionalPasskey};
