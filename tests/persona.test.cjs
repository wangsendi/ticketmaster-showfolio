"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { observePersona } = require("../src/persona.cjs");

function pageWith(urls, promptVisible) {
  return {
    frames: () => urls.map(url => ({ url: () => url })),
    getByText: pattern => {
      assert.ok(pattern.test("Verify Through Persona"));
      assert.equal(pattern.test("Verify your password"), false);
      return { first: () => ({ visible: promptVisible }) };
    },
  };
}

test("loading and password pages do not prove Persona verification", async () => {
  assert.deepEqual(await observePersona(pageWith(["https://auth.ticketmaster.com/as/authorization.oauth2"], false), async l => l.visible), {
    personaPageVisible: false, personaPromptVisible: false,
  });
});

test("the visible Verify Through Persona prompt proves identity verification", async () => {
  assert.deepEqual(await observePersona(pageWith(["https://auth.ticketmaster.com/as/authorization.oauth2"], true), async l => l.visible), {
    personaPageVisible: false, personaPromptVisible: true,
  });
});

test("Persona frames match the actual Persona host", async () => {
  const check = async urls => observePersona(pageWith(urls, false), async l => l.visible);
  assert.equal((await check(["https://inquiry.withpersona.com/verify"])).personaPageVisible, true);
  assert.equal((await check(["https://example.invalid/?inquiry.withpersona.com"])).personaPageVisible, false);
});
