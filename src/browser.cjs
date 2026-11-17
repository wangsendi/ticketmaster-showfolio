'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { chromium, firefox } = require('playwright');
const CLOAK_VERSION = '146.0.7680.177.5';
const DEFAULT_LOCALE = 'zh-CN';

function configuredBrowser(fallback = process.env.TM_BROWSER || 'cloakbrowser', configPath = process.env.TM_BROWSER_CONFIG || path.join(__dirname, '../data/browser.json')) {
  if (process.env.TM_BROWSER_OVERRIDE !== undefined) {
    return browserConfig(process.env.TM_BROWSER_OVERRIDE).name;
  }
  try {
    const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    if (!config || typeof config.browser !== 'string') throw new Error('invalid browser config');
    return config.browser.trim().toLowerCase();
  } catch (error) {
    if (error.code === 'ENOENT') return fallback;
    throw new Error('浏览器配置文件无效，请检查 data/browser.json。');
  }
}

function cloakCache() {
  return process.env.CLOAKBROWSER_CACHE_DIR || path.join(__dirname, '../data/browser-cache');
}

function cloakBinaryPath() {
  if (process.env.CLOAKBROWSER_BINARY_PATH) return process.env.CLOAKBROWSER_BINARY_PATH;
  const executable = process.platform === 'win32' ? 'chrome.exe' : process.platform === 'darwin' ? 'Chromium.app/Contents/MacOS/Chromium' : 'chrome';
  return path.join(cloakCache(), `chromium-${CLOAK_VERSION}`, executable);
}

async function cloakModule() {
  process.env.CLOAKBROWSER_CACHE_DIR ||= cloakCache();
  process.env.CLOAKBROWSER_AUTO_UPDATE = 'false';
  return import('cloakbrowser');
}

async function launchCloak(options) {
  const {launch} = await cloakModule();
  return launch({headless:options.headless, proxy:options.proxy,
    locale:options.locale || DEFAULT_LOCALE, timezone:options.timezone || 'America/New_York',
    browserVersion:CLOAK_VERSION, launchOptions:{timeout:options.timeout || 30000}});
}

function browserConfig(value = 'cloakbrowser') {
  const name = String(value).trim().toLowerCase();
  if (name === 'cloakbrowser') return {name, type:{launch:launchCloak, executablePath:cloakBinaryPath}, launchOptions:{}};
  if (name === 'firefox') return { name, type: firefox, launchOptions: {} };
  if (name === 'chromium') return { name, type: chromium, launchOptions: {} };
  if (name === 'chrome') return { name, type: chromium, launchOptions: { channel: 'chrome' } };
  throw new Error('浏览器必须是 cloakbrowser、firefox、chromium 或 chrome。');
}

function assertBrowserInstalled(value) {
  const config = browserConfig(value);
  if (config.name !== 'chrome') {
    if (!fs.existsSync(config.type.executablePath())) {
      if (config.name === 'cloakbrowser') throw new Error('未安装 CloakBrowser，请先运行 npm run install:cloakbrowser。');
      throw new Error(`未安装 ${config.name}，请先运行 npx playwright install ${config.name}。`);
    }
  } else {
    const candidates = process.platform === 'darwin'
      ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
      : process.platform === 'win32'
        ? [process.env.PROGRAMFILES, process.env['PROGRAMFILES(X86)'], process.env.LOCALAPPDATA]
          .filter(Boolean).map(root => path.join(root, 'Google/Chrome/Application/chrome.exe'))
        : ['/opt/google/chrome/chrome', '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable'];
    const executablePath = candidates.find(candidate => fs.existsSync(candidate));
    if (!executablePath) throw new Error('未安装 Google Chrome；可先使用 TM_BROWSER=chromium，或安装 Chrome 后重启服务。');
    config.launchOptions.executablePath = executablePath;
  }
  return config;
}

async function createBrowserContext(selected, browser, options = {}) {
  if (selected.name !== 'cloakbrowser') return browser.newContext(options);
  const {buildContextOptions} = await cloakModule();
  // Locale/timezone are supplied at launch, matching the isolated probe.
  const {locale, timezoneId, viewport, ...contextOptions} = options;
  return browser.newContext(buildContextOptions({headless:false, contextOptions}));
}

module.exports = { browserConfig, assertBrowserInstalled, configuredBrowser, createBrowserContext, cloakModule, CLOAK_VERSION, DEFAULT_LOCALE };
