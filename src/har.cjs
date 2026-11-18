"use strict";

// Filter the recording only: page traffic and resource loading stay untouched.
function loginHarFilter(startURL) {
  const campaign = new URL(startURL).pathname.replace(/\/$/, "");
  const escaped = campaign.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(
    "^https://(?:" +
    "auth\\.ticketmaster\\.com/json/(?:account-status-validation|sign-in)(?:[?#]|$)|" +
    "identity\\.ticketmaster\\.com/exchange(?:[?#]|$)|" +
    "signup\\.ticketmaster\\.com(?:/graphql|" + escaped + "/?)(?:[?#]|$)|" +
    // Keep every CA request so the offline audit can still count them.
    "(?:[a-z0-9-]+\\.)*ticketmaster\\.ca(?::443)?(?:/|$))", "i");
}

module.exports = { loginHarFilter };
