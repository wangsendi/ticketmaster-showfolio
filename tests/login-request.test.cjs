'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {setTimeout:wait} = require('node:timers/promises');
const {submitAndWaitForResponse,businessError,passwordFormState,emailAccountIssue,passwordFormIssue,passwordResetVisible} = require('../src/login-request.cjs');

test('registers the matching POST response before submitting', async()=>{
  let waiting = false;
  const response = {status:()=>200};
  const page = {waitForResponse(predicate) {
    waiting = true;
    assert.equal(predicate({url:()=> 'https://auth.ticketmaster.com/json/sign-in',request:()=>({method:()=> 'POST'})}),true);
    assert.equal(predicate({url:()=> 'https://auth.ticketmaster.com/json/sign-in',request:()=>({method:()=> 'GET'})}),false);
    return Promise.resolve(response);
  }};
  assert.equal(await submitAndWaitForResponse(page,'/json/sign-in',async()=>{assert.equal(waiting,true);}),response);
});

test('click failure followed by response waiter rejection does not crash the process', async()=>{
  const page = {waitForResponse:()=>wait(10).then(()=>{throw new Error('Target page has been closed');})};
  await assert.rejects(submitAndWaitForResponse(page,'/json/sign-in',async()=>{throw new Error('click failed');}),/click failed/);
  await wait(30);
});

test('response failure followed by click rejection is also handled', async()=>{
  const page = {waitForResponse:()=>Promise.reject(new Error('response timed out'))};
  await assert.rejects(submitAndWaitForResponse(page,'/json/sign-in',()=>wait(10).then(()=>{throw new Error('late click failure');})),/response timed out/);
  await wait(30);
});

test('records only redacted primitive business errors, including email validation errors', ()=>{
  const body = {errorCode:'PASSWORD_RESET_REQUIRED',responseCode:409,message:'Reset for sample@example.invalid',cookies:'private',token:'private',errorType:{private:'nested'}};
  assert.deepEqual(businessError(body,text=>text.replace('sample@example.invalid','<email>')),
    {errorCode:'PASSWORD_RESET_REQUIRED',message:'Reset for <email>',responseCode:409});
  assert.deepEqual(businessError(null),{});
});

test('password diagnostics retain validation state without retaining the password', async()=>{
  const page={locator(selector){
    if(selector==='input[type="password"]') return {evaluate:fn=>fn({value:'fixture',minLength:8,validity:{valid:false,tooShort:true,patternMismatch:false}})};
    return {isEnabled:()=>false};
  }};
  assert.deepEqual(await passwordFormState(page),{valueLength:7,minLength:8,valid:false,tooShort:true,patternMismatch:false,submitEnabled:false});
});

test('only explicit email business errors identify terminal account issues',()=>{
  assert.equal(emailAccountIssue({message:'The user is not found',responseCode:404}),'account-not-found');
  assert.equal(emailAccountIssue({message:'Password reset required',responseCode:409}),'password-reset-required');
  for(const body of [null,{}, {responseCode:404},{responseCode:409},{message:'Forbidden'}]) assert.equal(emailAccountIssue(body),null);
});

test('records short-password disabled submission without guessing that every disabled button is a bad password',()=>{
  assert.equal(passwordFormIssue({valueLength:7,submitEnabled:false}),'password-submit-disabled');
  assert.equal(passwordFormIssue({valueLength:5,submitEnabled:false,tooShort:true}),'password-format-invalid');
  assert.equal(passwordFormIssue({valueLength:12,submitEnabled:false}),null);
  assert.equal(passwordFormIssue({valueLength:7,submitEnabled:true}),null);
});

test('password reset is recognized only from a visible explicit reset message',async()=>{
  for(const visible of [false,true]) {
    const page={getByText(pattern){assert.equal(pattern.test('Set up a new password and get back to accessing all the best'),true);return {first:()=>({isVisible:async()=>visible})};}};
    assert.equal(await passwordResetVisible(page),visible);
  }
});
