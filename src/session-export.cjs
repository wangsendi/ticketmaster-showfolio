"use strict";

async function readSession(context) {
  // storageState already contains the complete cookie jar. Keep the browser's
  // URL filtering for the exported signup cookies (domain/path/secure rules).
  const [signup, storageState] = await Promise.all([
    context.cookies("https://signup.ticketmaster.com/"),
    context.storageState(),
  ]);
  const caCookieCount = storageState.cookies.filter(cookie =>
    cookie.domain === "ticketmaster.ca" || cookie.domain.endsWith(".ticketmaster.ca"),
  ).length;
  return { signup, storageState, caCookieCount };
}

module.exports = { readSession };
