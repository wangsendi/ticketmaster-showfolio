"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { loginHarFilter } = require("./har.cjs");
const { readSession } = require("./session-export.cjs");
const { dismissOptionalPasskey } = require("./passkey.cjs");
const { assertBrowserInstalled, configuredBrowser, createBrowserContext, DEFAULT_LOCALE } = require("./browser.cjs");
const { capturePageWithRefresh, ensureAccountPageWithRefresh, captureFailurePage } = require("./capture.cjs");
const { submitAndWaitForResponse, waitForSubmitReady, businessError, passwordFormState, emailAccountIssue, passwordFormIssue, passwordResetVisible } = require("./login-request.cjs");
const { credentialsRejected } = require("./credentials.cjs");
const { observePersona } = require("./persona.cjs");
const { enterOfficialLogin, EMAIL_SELECTOR, isBrowsingPaused } = require("./login-navigation.cjs");
const { createBridge } = require("./proxy-bridge.cjs");

async function main() {
  process.umask(0o077);
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  if (
    typeof input.email !== "string" ||
    !input.email ||
    typeof input.password !== "string" ||
    !input.password
  ) {
    throw new Error("Missing account input");
  }
  const startURL = new URL(
    input.startURL || "https://signup.ticketmaster.com/noahkahan",
  );
  if (
    startURL.protocol !== "https:" ||
    startURL.host !== "signup.ticketmaster.com" ||
    startURL.search ||
    startURL.hash
  ) {
    throw new Error("Expected an official COM signup campaign URL");
  }
  if (!input.outputDirectory) throw new Error("Temporary working directory required");
  const out = input.outputDirectory;
  fs.mkdirSync(out, { recursive: true, mode: 0o700 });
  const result = {
    startedAtUTC: new Date().toISOString(),
    headless: false,
    locale: DEFAULT_LOCALE,
    usesXvfb: !!input.usesXvfb,
    networkMode: input.networkMode || "existing_system_network",
    networkConfigurationChanged: false,
    startURL: startURL.href,
    officialSignInButton: "Sign In",
    explicitProxy: !!input.proxy,
    ...(input.proxy ? { proxyNode: input.proxyNode } : {}),
    callbackOverridden: false,
    countryHeadersOverridden: false,
    attempts: { email: 0, password: 0, optionalPasskey: 0 },
    accountRequestsAllowed: { email: 0, password: 0 },
    caRequestCount: 0,
    responses: [],
    exchangeCOMObserved: false,
    fanIsLoggedIn: null,
    fanAccountMatches: false,
    requiresHumanInteraction: false,
    uiObservations: [],
    captchaResources: [],
    pageErrorCount: 0,
    pageLifecycle: [],
    phaseDurationsMs: {},
  };
  let browser, context, page, proxyBridge;
  let verifiedCookieHeader = null;
  let cleanupStarted = false;
  let stage = "launch";
  const stopped = () =>
    fs.existsSync(path.join(out, "STOP")) ||
    !!(input.stopFile && fs.existsSync(input.stopFile));
  const checkStopped = () => {
    if (stopped()) throw new Error("Stopped at user request");
    if (result.requiresHumanInteraction)
      throw new Error("Visible verification requires human interaction");
  };
  const safePath = (raw) => {
    try {
      const u = new URL(raw);
      return `${u.hostname}${u.pathname}`;
    } catch {
      return null;
    }
  };
  const redact = (text) =>
    String(text || "")
      .replaceAll(input.email, "<email>")
      .replaceAll(input.password, "<password>")
      .replace(/https?:\/\/[^\s"'<>]+/g, (raw) => {
        try {
          const u = new URL(raw);
          return `${u.protocol}//${u.host}${u.pathname}${u.search ? "?<redacted>" : ""}`;
        } catch {
          return "<url>";
        }
      })
      .replace(/\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, "<email>")
      .replace(/[A-Za-z0-9_-]{80,}/g, "<token>")
      .slice(0, 1200);
  let inspecting = false;
  async function observeUI(label) {
    if (!page || page.isClosed() || inspecting) return;
    inspecting = true;
    try {
      const observation = {
        atUTC: new Date().toISOString(),
        stage,
        label,
        url: safePath(page.url()),
        imageChallengeVisible: false,
        checkboxChallengeVisible: false,
        otpInputVisible: false,
        personaPageVisible: false,
        recaptchaFrameCount: 0,
      };
      async function onScreen(locator) {
        if (!(await locator.isVisible().catch(() => false))) return false;
        const box = await locator.boundingBox().catch(() => null);
        return (
          !!box &&
          box.width > 0 &&
          box.height > 0 &&
          box.x < 1440 &&
          box.y < 1000 &&
          box.x + box.width > 0 &&
          box.y + box.height > 0
        );
      }
      for (const frame of page.frames()) {
        const u = frame.url();
        if (/google\.com\/recaptcha\//.test(u)) {
          observation.recaptchaFrameCount++;
          if (await onScreen(frame.locator(".rc-imageselect").first()))
            observation.imageChallengeVisible = true;
          if (await onScreen(frame.locator("#recaptcha-anchor").first()))
            observation.checkboxChallengeVisible = true;
        }
      }
      Object.assign(observation, await observePersona(page, onScreen));
      if (observation.personaPageVisible || observation.personaPromptVisible) {
        result.personaVerificationRequired = true;
        result.verificationRequired = true;
      }
      observation.otpInputVisible = await onScreen(
        page
          .locator(
            'input[autocomplete="one-time-code"],input[name="otp"],input[name="verificationCode"]',
          )
          .first(),
      );
      observation.passwordResetVisible = await passwordResetVisible(page);
      if (observation.passwordResetVisible) {
        result.passwordResetRequired = true;
        result.accountIssue = "password-reset-required";
      }
      observation.browsingActivityPaused = await isBrowsingPaused(page);
      const human =
        observation.passwordResetVisible ||
        observation.browsingActivityPaused ||
        observation.imageChallengeVisible ||
        observation.checkboxChallengeVisible ||
        observation.otpInputVisible ||
        observation.personaPageVisible ||
        observation.personaPromptVisible;
      if (human) {
        const file = observation.browsingActivityPaused ? "observed-verification.png" : label.replace(/[^a-z0-9-]/gi, "_") + ".png";
        await page
          .screenshot({ path: path.join(out, file), timeout: 5000 })
          .catch(() => {});
        observation.screenshot = file;
      }
      result.uiObservations.push(observation);
      if (human && !result.requiresHumanInteraction) {
        result.requiresHumanInteraction = true;
        result.browsingActivityPaused = observation.browsingActivityPaused;
        result.humanVerificationObservation = observation;
        // 此轮只观察验证要求；关掉页面以阻止后续自动提交，context仍用于落盘证据。
        await page.close().catch(() => {});
      }
    } catch {
    } finally {
      inspecting = false;
    }
  }
  let observer;
  const progress = setInterval(() => {
    fs.writeFileSync(
      path.join(out, "progress.json"),
      JSON.stringify(
        {
          stage,
          attempts: result.attempts,
          accountRequestsAllowed: result.accountRequestsAllowed,
          oauthCallbackHost: result.oauthCallbackHost || null,
          emailStatus: result.emailStatus || null,
          passwordStatus: result.passwordStatus || null,
          exchangeCOMObserved: result.exchangeCOMObserved,
          requiresHumanInteraction: result.requiresHumanInteraction,
          recaptchaInitialization: result.recaptchaInitialization || null,
          lastUIObservation: result.uiObservations.at(-1) || null,
          atUTC: new Date().toISOString(),
        },
        null,
        2,
      ) + "\n",
      { mode: 0o600 },
    );
  }, 2000);
  progress.unref();
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const pending = [];
  try {
    const selected = assertBrowserInstalled(configuredBrowser(input.browser || "firefox"));
    result.browser = selected.name;
    const launchOptions = { ...selected.launchOptions, headless: false, timeout: 30000 };
    if (selected.name === "cloakbrowser") {
      // New children use the current locale even if the Python service still sends an old default.
      launchOptions.locale = DEFAULT_LOCALE;
      launchOptions.timezone = input.timezoneId || "America/New_York";
    }
    if (input.proxy) {
      stage = "proxy_setup";
      proxyBridge = createBridge(input.proxy);
      await new Promise((resolve, reject) => {
        proxyBridge.once("error", reject);
        proxyBridge.listen(0, "127.0.0.1", resolve);
      });
      launchOptions.proxy = { server: `http://127.0.0.1:${proxyBridge.address().port}` };
    }
    stage = "launch";
    browser = await selected.type.launch(launchOptions);
    browser.on("disconnected", () =>
      result.pageLifecycle.push({
        event: "browser_disconnected",
        cleanupStarted,
        atUTC: new Date().toISOString(),
      }),
    );
    result.browserVersion = browser.version();
    result.displayEnvironmentPresent = !!process.env.DISPLAY;
    context = await createBrowserContext(selected, browser, {
      viewport: { width: 1440, height: 1000 },
      locale: DEFAULT_LOCALE,
      timezoneId: input.timezoneId || "America/New_York",
      recordHar: {
        path: path.join(out, "com-login.har"),
        content: "embed",
        mode: "full",
        urlFilter: loginHarFilter(startURL.href),
      },
    });
    context.on("request", (request) => {
      const host = new URL(request.url()).hostname;
      if (host === "ticketmaster.ca" || host.endsWith(".ticketmaster.ca"))
        result.caRequestCount++;
    });
    await context.route("**/*", async (route) => {
      const request = route.request(),
        u = new URL(request.url());
      if (
        request.method() === "POST" &&
        u.hostname === "auth.ticketmaster.com"
      ) {
        const key =
          u.pathname === "/json/account-status-validation"
            ? "email"
            : u.pathname === "/json/sign-in"
              ? "password"
              : null;
        if (key) {
          if (
            stopped() ||
            result.requiresHumanInteraction ||
            result.accountRequestsAllowed[key] >= 1
          )
            return route.abort("blockedbyclient");
          result.accountRequestsAllowed[key]++;
        }
      }
      return route.continue();
    });
    page = await context.newPage();
    page.on("crash", () => {
      result.pageCrashObserved = true;
      result.pageLifecycle.push({
        event: "page_crash",
        cleanupStarted,
        atUTC: new Date().toISOString(),
      });
    });
    page.on("close", () =>
      result.pageLifecycle.push({
        event: "page_close",
        cleanupStarted,
        humanVerificationStop: result.requiresHumanInteraction,
        atUTC: new Date().toISOString(),
      }),
    );
    observer = setInterval(() => {
      observeUI("observed-verification").catch(() => {});
    }, 2000);
    observer.unref();
    page.on("pageerror", () => {
      result.pageErrorCount++;
    });
    page.on("console", (message) => {
      if (/recaptcha|captcha|verification/i.test(message.text())) {
        result.captchaConsole = result.captchaConsole || [];
        if (result.captchaConsole.length < 20)
          result.captchaConsole.push({
            type: message.type(),
            text: redact(message.text()),
            atUTC: new Date().toISOString(),
          });
      }
    });
    page.on("requestfailed", (request) => {
      if (/\/recaptcha\//.test(request.url()))
        result.captchaResources.push({
          phase: "requestfailed",
          url: safePath(request.url()),
          error: request.failure()?.errorText,
        });
    });
    page.on("response", (response) => {
      const u = new URL(response.url()),
        request = response.request();
      if (/\/recaptcha\//.test(u.pathname))
        result.captchaResources.push({
          phase: "response",
          url: safePath(response.url()),
          status: response.status(),
          atUTC: new Date().toISOString(),
        });
      if (
        (u.hostname === "auth.ticketmaster.com" &&
          u.pathname.startsWith("/json/")) ||
        u.pathname === "/exchange"
      ) {
        const item = {
          host: u.hostname,
          path: u.pathname,
          method: request.method(),
          status: response.status(),
          atUTC: new Date().toISOString(),
        };
        result.responses.push(item);
        pending.push(
          (async () => {
            try {
              const json = await response.json();
              item.responseFieldNames = Object.keys(json).sort();
              if (u.pathname === "/json/sign-in" && json.flow === "add-passkey")
                result.optionalPasskey = true;
              if (json.inquiryData) result.inquiryDataObserved = true;
              if (u.pathname === "/json/sign-in") {
                try {
                  const data = JSON.parse(request.postData() || "{}");
                  const token = data.recaptchaToken;
                  result.passwordRecaptchaToken = {
                    type: token === null ? "null" : typeof token,
                    nonempty: typeof token === "string" && token.length > 0,
                    length: typeof token === "string" ? token.length : null,
                  };
                } catch {}
              }
              if (u.pathname === "/json/sign-in" || u.pathname === "/json/account-status-validation")
                item.businessError = businessError(json, redact);
            } catch {}
          })(),
        );
      }
      if (
        u.hostname === "identity.ticketmaster.com" &&
        u.pathname === "/exchange" &&
        response.status() >= 200 &&
        response.status() < 400
      ) {
        pending.push(
          response
            .finished()
            .then((error) => {
              if (!error) result.exchangeCOMObserved = true;
            })
            .catch(() => {}),
        );
      }
      if (
        u.hostname === "signup.ticketmaster.com" &&
        u.pathname === "/graphql"
      ) {
        pending.push(
          (async () => {
            try {
              const json = await response.json();
              result.fanStatus = response.status();
              const fan = json?.data?.fan;
              if (typeof fan?.isLoggedIn === "boolean")
                result.fanIsLoggedIn = fan.isLoggedIn;
              if (fan?.isLoggedIn === true) {
                result.fanEntryRecordPresent = !!fan.entryRecord;
                result.fanAccountMatches =
                  typeof fan.email === "string" &&
                  fan.email.toLowerCase() === input.email.toLowerCase();
                if (result.fanAccountMatches && response.status() === 200) {
                  const header = await request.headerValue("cookie");
                  if (header) {
                    verifiedCookieHeader = header;
                    result.validatedFan = {
                      host: u.hostname,
                      path: u.pathname,
                      status: response.status(),
                      atUTC: new Date().toISOString(),
                    };
                  }
                }
              }
            } catch {}
          })(),
        );
      }
    });
    stage = "campaign";
    await page.goto(result.startURL, {
      waitUntil: "domcontentloaded",
      timeout: 120000,
    });
    await observeUI("campaign");
    const expectedHost = new URL(result.startURL).hostname;
    if (new URL(page.url()).hostname !== expectedHost)
      throw new Error("Unexpected starting domain");
    result.loginNavigation = {};
    await enterOfficialLogin(page, {
      onStage: value => { stage = value; },
      checkStopped,
      diagnostics: result.loginNavigation,
    });
    const selector = EMAIL_SELECTOR;
    await observeUI("email-ready");
    // Confirm library availability before submitting any account information.
    // One ordinary page reload is allowed only for an observed transport
    // failure of the public classic library on an already visible login form.
    result.preEmailRecaptcha = [];
    const waitBeforeEmail = async (label) => {
      const observation = {
        label,
        startedAtUTC: new Date().toISOString(),
        ready: false,
      };
      result.preEmailRecaptcha.push(observation);
      const started = Date.now();
      try {
        await page.waitForFunction(
          () => typeof window.grecaptcha?.execute === "function",
          undefined,
          { timeout: 45000 },
        );
        observation.ready = true;
      } finally {
        observation.elapsedMs = Date.now() - started;
        observation.observedState = await page
          .evaluate(() => ({
            namespace: typeof window.grecaptcha,
            execute: typeof window.grecaptcha?.execute,
            enterpriseExecute: typeof window.grecaptcha?.enterprise?.execute,
          }))
          .catch(() => null);
      }
    };
    stage = "wait_recaptcha_before_email";
    try {
      await waitBeforeEmail("initial_login_page");
    } catch (error) {
      checkStopped();
      const classicTransportFailure = result.captchaResources.some(
        (item) =>
          item.phase === "requestfailed" &&
          item.url === "www.google.com/recaptcha/api.js" &&
          /CONNECTION_REFUSED|TLS|timed out|TIMEOUT/i.test(item.error || ""),
      );
      if (
        error.name !== "TimeoutError" ||
        !classicTransportFailure ||
        page.isClosed()
      )
        throw error;
      stage = "reload_login_once";
      result.loginReloads = 1;
      await page.reload({ waitUntil: "domcontentloaded", timeout: 60000 });
      await page
        .locator(selector)
        .first()
        .waitFor({ state: "visible", timeout: 240000 });
      await observeUI("login-reloaded");
      checkStopped();
      stage = "wait_recaptcha_before_email";
      await waitBeforeEmail("after_one_normal_reload");
    }
    checkStopped();
    const loginURL = new URL(page.url());
    const callback = loginURL.searchParams.get("redirect_uri");
    result.loginHost = loginURL.hostname;
    result.oauthCallbackHost = callback ? new URL(callback).hostname : null;
    result.oauthCallbackPath = callback ? new URL(callback).pathname : null;
    if (
      loginURL.hostname !== "auth.ticketmaster.com" ||
      result.oauthCallbackHost !== "identity.ticketmaster.com"
    ) {
      throw new Error("Official login callback is not COM");
    }
    stage = "email_once";
    checkStopped();
    await page.locator(selector).first().fill(input.email, { timeout: 15000 });
    stage = "wait_email_submit";
    result.emailSubmitReadiness = {};
    await waitForSubmitReady(page, { diagnostics: result.emailSubmitReadiness, checkStopped });
    stage = "email_once";
    result.attempts.email++;
    const email = await submitAndWaitForResponse(page, "/json/account-status-validation", () =>
      page.locator('button[type="submit"]').click({ timeout: 15000, noWaitAfter: true }));
    result.emailStatus = email.status();
    await observeUI("after-email");
    if (email.status() !== 200) {
      result.emailBusinessError = businessError(await email.json().catch(() => null), redact);
      const issue = emailAccountIssue(result.emailBusinessError);
      if (issue) {
        result.accountIssue = issue;
        result.requiresHumanInteraction = true;
      }
      const detail = result.emailBusinessError.message || result.emailBusinessError.errorType;
      throw new Error(`Email request not accepted (HTTP ${email.status()})${detail ? ": " + detail : ""}`);
    }
    stage = "password_once";
    checkStopped();
    await page
      .locator('input[type="password"]')
      .waitFor({ state: "visible", timeout: 60000 });
    await observeUI("password-ready");
    checkStopped();
    await page
      .locator('input[type="password"]')
      .fill(input.password, { timeout: 15000 });
    result.passwordFormAfterFill = await passwordFormState(page);
    // The password field can become visible before the site's async CAPTCHA
    // runtime has loaded. Observe its public API; let the site's own click
    // handler perform the verification. Never create or inject a token here.
    stage = "wait_recaptcha_initialization";
    const captchaWaitStarted = Date.now();
    result.recaptchaInitialization = {
      waitStartedAtUTC: new Date().toISOString(),
      timeoutMs: 45000,
      ready: false,
    };
    try {
      await page.waitForFunction(
        () => typeof window.grecaptcha?.execute === "function",
        undefined,
        { timeout: 45000 },
      );
      result.recaptchaInitialization.ready = true;
    } finally {
      result.recaptchaInitialization.elapsedMs =
        Date.now() - captchaWaitStarted;
      result.recaptchaInitialization.observedState = await page
        .evaluate(() => ({
          namespace: typeof window.grecaptcha,
          ready: typeof window.grecaptcha?.ready,
          execute: typeof window.grecaptcha?.execute,
        }))
        .catch(() => null);
    }
    checkStopped();
    await observeUI("recaptcha-ready");
    checkStopped();
    stage = "password_once";
    result.passwordFormBeforeSubmit = await passwordFormState(page);
    const formIssue = passwordFormIssue(result.passwordFormBeforeSubmit);
    if (formIssue) {
      result.accountIssue = formIssue;
      result.requiresHumanInteraction = true;
      throw new Error(formIssue === "password-submit-disabled" ? "密码仅7位，登录按钮不可用，请核对密码。" : "密码格式不符合网站要求，请核对密码。");
    }
    stage = "wait_password_submit";
    result.passwordSubmitReadiness = {};
    await waitForSubmitReady(page, { diagnostics: result.passwordSubmitReadiness, checkStopped });
    stage = "password_once";
    result.attempts.password++;
    const password = await submitAndWaitForResponse(page, "/json/sign-in", () =>
      page.locator('button[type="submit"]').click({ timeout: 15000, noWaitAfter: true }));
    result.passwordStatus = password.status();
    const passwordBody = await password.json().catch(() => null);
    if (passwordBody?.flow === "add-passkey") result.optionalPasskey = true;
    if (credentialsRejected(passwordBody)) {
      result.credentialsRejected = true;
      throw new Error("账号或密码错误，网站提示登录凭据不匹配。");
    }
    await observeUI("after-password");
    if (password.status() !== 200)
      throw new Error("Password request not accepted");
    stage = "wait_exchange";
    result.passkeyDismissal = {attempts: [], completed: false};
    const until = Date.now() + 60000;
    while (Date.now() < until && !result.exchangeCOMObserved) {
      checkStopped();
      if (result.verificationRequired) break;
      if (result.optionalPasskey) {
        await dismissOptionalPasskey(page, result.passkeyDismissal, {
          checkStopped,
          onAttempt: () => { stage = "dismiss_passkey"; result.attempts.optionalPasskey++; },
        });
        stage = "wait_exchange";
      }
      await wait(500);
    }
    result.passkeyDismissal.completed = result.exchangeCOMObserved;
    if (!result.exchangeCOMObserved) {
      if (result.optionalPasskey) {
        const error = new Error(result.passkeyDismissal.attempts.some(item => item.clicked)
          ? "已点击暂不添加 Passkey，但登录回调未完成。"
          : "添加 Passkey 页面未就绪，未能点击暂不添加，登录回调未完成。");
        error.name = "PasskeyCallbackTimeout";
        throw error;
      }
      throw new Error("COM exchange not observed");
    }
    stage = "wait_com_return";
    await page.waitForURL((url) => url.origin === startURL.origin && url.pathname === startURL.pathname, {
      waitUntil: "domcontentloaded",
      timeout: 45000,
    });
    const current = new URL(page.url());
    result.finalHost = current.hostname;
    result.finalPath = current.pathname;
    stage = "readonly_com_fan";
    // Wait for the complete account identity and Cookie, not only isLoggedIn.
    const waitForVerifiedFan = async () => {
      const until = Date.now() + 30000;
      while (Date.now() < until) {
        checkStopped();
        if (result.fanIsLoggedIn === true && !result.fanAccountMatches)
          throw new Error("Fan account did not match the submitted account");
        if (result.fanIsLoggedIn === true && result.fanAccountMatches && verifiedCookieHeader) break;
        await wait(500);
      }
      await Promise.allSettled(pending);
      checkStopped();
      if (result.fanIsLoggedIn === true && !result.fanAccountMatches)
        throw new Error("Fan account did not match the submitted account");
      const current = new URL(page.url());
      if (current.origin !== startURL.origin || current.pathname !== startURL.pathname)
        throw new Error("Campaign page changed during account verification");
      return result.fanIsLoggedIn === true && result.fanAccountMatches && !!verifiedCookieHeader;
    };
    Object.assign(result, await ensureAccountPageWithRefresh(page, {
      checkStopped,
      verify: waitForVerifiedFan,
      onRefresh: (count) => {
        stage = "refresh_account_page";
        result.accountPageRefreshes = count;
        result.fanIsLoggedIn = null;
        result.fanAccountMatches = false;
        verifiedCookieHeader = null;
      },
    }));
    stage = "capture_page";
    Object.assign(result, await capturePageWithRefresh(page, path.join(out, "com-result.png"), checkStopped, {
      onRefresh: (count) => {
        result.screenshotRefreshes = count;
        result.fanIsLoggedIn = null;
        result.fanAccountMatches = false;
        verifiedCookieHeader = null;
      },
      afterRefresh: async () => {
        if (!(await waitForVerifiedFan())) {
          const error = new Error("Authenticated Fan response not observed after screenshot refresh");
          error.name = "TimeoutError";
          throw error;
        }
      },
    }));
    result.loginCompleted = true;
  } catch (error) {
    result.failureStage = stage;
    result.errorType = error.name || "Error";
    result.errorMessage = redact(error.message);
    if (page) Object.assign(result, await captureFailurePage(page, path.join(out, "failure.png"), { isStopped: stopped, redactError: redact }));
  } finally {
    cleanupStarted = true;
    clearInterval(observer);
    await observeUI("final-page");
    await Promise.allSettled(pending);
    if (context) {
      stage = "export_session";
      const exportStarted = performance.now();
      try {
        const { signup, storageState, caCookieCount } = await readSession(context);
        fs.writeFileSync(
          path.join(out, "cookies.json"),
          JSON.stringify(signup, null, 2) + "\n",
          { mode: 0o600 },
        );
        fs.writeFileSync(
          path.join(out, "storage-state.json"),
          JSON.stringify(storageState, null, 2) + "\n",
          { mode: 0o600 },
        );
        result.cookieCount = signup.length;
        result.signupCookieNames = signup.map((c) => c.name).sort();
        result.signupHasIDToken = signup.some((c) => c.name === "id-token");
        result.caCookieCount = caCookieCount;
      } catch (error) {
        result.cookieExportErrorType = error.name || "Error";
      } finally {
        result.phaseDurationsMs.sessionExport = Math.round(performance.now() - exportStarted);
      }
      result.authenticated =
        result.exchangeCOMObserved &&
        result.fanIsLoggedIn === true &&
        result.fanAccountMatches &&
        !!verifiedCookieHeader;
      if (result.authenticated) {
        fs.writeFileSync(
          path.join(out, "cookie-header.txt"),
          verifiedCookieHeader + "\n",
          { mode: 0o600 },
        );
        result.cookieHeaderSource =
          "Exact Cookie header of the authenticated Fan request";
      }
      stage = "close_context";
      const closeStarted = performance.now();
      await context.close().catch(() => {});
      result.phaseDurationsMs.contextClose = Math.round(performance.now() - closeStarted);
    }
    result.authenticated = !!result.authenticated;
    stage = "close_browser";
    const browserCloseStarted = performance.now();
    if (browser) await browser.close().catch(() => {});
    if (proxyBridge) {
      await proxyBridge.shutdown();
      result.proxyStats = proxyBridge.stats;
    }
    result.phaseDurationsMs.browserClose = Math.round(performance.now() - browserCloseStarted);
    clearInterval(progress);
    result.loginAttempts = { ...result.attempts };
    result.finishedAtUTC = new Date().toISOString();
    fs.writeFileSync(
      path.join(out, "result.json"),
      JSON.stringify(result, null, 2) + "\n",
      { mode: 0o600 },
    );
    console.log(JSON.stringify(result, null, 2));
  }
}
main().catch((error) => {
  console.log(JSON.stringify({ errorType: error.name }));
  process.exitCode = 1;
});
