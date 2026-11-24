/* Per-control browser diagnostics: a short console/network summary attached to a failing control only.
   It records event kinds, HTTP methods, paths (never query strings, headers, cookies or bodies). */
const MAX_EVENTS = 40;

function pathOf(url) {
  try { return new URL(url).pathname; } catch { return 'invalid-url'; }
}

function watch(page, events) {
  const add = (text) => { text = (Date.now() % 1000000) + ' ' + text; events.push(text.slice(0, 220)); if (events.length > MAX_EVENTS) events.shift(); };
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) add('console.' + m.type() + ' ' + m.text()); });
  page.on('pageerror', (e) => add('pageerror ' + (e && e.message)));
  page.on('requestfailed', (r) => add('requestfailed ' + r.method() + ' ' + pathOf(r.url()) + ' ' + ((r.failure() || {}).errorText || '')));
  page.on('response', (r) => { if (r.status() >= 400 || r.request().method() !== 'GET') add('http ' + r.status() + ' ' + r.request().method() + ' ' + pathOf(r.url())); });
  page.on('framenavigated', (f) => { if (f === page.mainFrame()) add('navigated ' + pathOf(f.url())); });
}

/** Watch every context a control creates through the shared `browser` fixture. */
function install(test) {
  const state = new WeakMap();
  test.beforeEach(async ({ browser }, testInfo) => {
    const events = [], original = browser.newContext;
    state.set(testInfo, { browser, original, events });
    browser.newContext = async (...args) => {
      const context = await original.apply(browser, args);
      context.on('page', (page) => watch(page, events));
      return context;
    };
  });
  test.afterEach(async ({}, testInfo) => {
    const saved = state.get(testInfo);
    if (!saved) return;
    saved.browser.newContext = saved.original;
    if (testInfo.status !== 'passed' && saved.events.length) {
      testInfo.annotations.push({ type: 'browser-summary', description: saved.events.join('\n') });
    }
  });
}

module.exports = { install };
