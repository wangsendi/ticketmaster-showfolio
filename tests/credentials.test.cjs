'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {credentialsRejected} = require('../src/credentials.cjs');

test('recognizes explicit credential mismatch inside an HTTP 200 business response', () => {
  assert.equal(credentialsRejected({errorType:'Operation Not Allowed', message:'Credentials mismatch 1 attempts used', responseCode:403}), true);
  for (const body of [null, {}, {message:'Forbidden', responseCode:403}, {message:'Browsing Activity Paused'}, {flow:'add-passkey'}, {message:'COM exchange not observed'}]) {
    assert.equal(credentialsRejected(body), false);
  }
});
