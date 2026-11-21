/* global showDialog, api, state */
// The ONE module that knows how the frontend signs a person in and out.
//
// The frontend signs in with "Continue with Google" -> /api/v0/account/google/start and keeps
// no token in browser storage: the account session is the HttpOnly __Host-weaves_account_session
// cookie plus an X-Weaves-CSRF header. These helpers drive the real redirect flow against the
// harness' fake Google provider (identity chosen through a private control file) and then use
// the application's own organization chooser/switcher. Every selector or application global that
// the new UI may name differently lives in SELECTORS or in a function below, marked
// TODO(frontend-onboarding); controls never hard-code them.
const { expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');

const SESSION_COOKIE = '__Host-weaves_account_session';

// TODO(frontend-onboarding): confirm each selector against the new onboarding frontend. The
// organization chooser/switcher names below are the existing ones, kept until proven otherwise.
const SELECTORS = {
  continueWithGoogle: (page) => page.getByRole('button', { name: /Continue with Google/ }),
  chooserHeading: (page) => page.getByRole('heading', { name: 'Choose an organization' }),
  chooseOrganization: (org) => `[data-account-action="choose"][data-organization-id="${org}"]`,
  profileMenuToggle: '#profile-menu-toggle',
  switchOrganization: (org) => `[data-account-action="switch"][data-organization-id="${org}"]`,
  signOut: '[data-action="logout"]',
};

const appUrl = () => process.env.WEAVES_E2E_APP_URL;
const identityFile = () => {
  const dir = process.env.TENANCY_GOOGLE_DIR || path.join(path.dirname(process.env.TENANCY_FIXTURE_FILE), 'google');
  return path.join(dir, 'identity.json');
};

// Choose who the fake Google provider signs in next (its authorize endpoint reads this file).
// The provider runs in a container with a bind mount, so wait until it reports the new choice.
async function chooseGoogleIdentity(email, extra = {}) {
  const file = identityFile();
  const temporary = file + '.tmp';
  const identity = { email, ...extra };
  fs.writeFileSync(temporary, JSON.stringify(identity), { mode: 0o600 });
  fs.renameSync(temporary, file);
  await expect.poll(async () => {
    const response = await fetch(appUrl() + '/fake-google/identity');
    return response.json();
  }, { timeout: 15000 }).toEqual(identity);
}

// Sign in by following the real redirect chain: start -> proxied fake authorize -> callback
// (sets the session cookie) -> return_to. The page ends on the application at `route`.
async function signInWithGoogle(page, email, route = '') {
  await chooseGoogleIdentity(email);
  const returnTo = '/' + (route ? '#' + route : '');
  await page.goto(appUrl() + '/api/v0/account/google/start?return_to=' + encodeURIComponent(returnTo));
}

// TODO(frontend-onboarding): the application's selected-organization state. The existing SPA
// exposes `state.organization.id`; a rewritten frontend may expose it elsewhere.
const currentOrganizationId = (page) => page.evaluate(() => (typeof state === 'undefined' ? '' : state.organization?.id || ''));

// Use the application's own chooser when it asks, otherwise accept an automatic selection.
async function chooseOrganization(page, organizationId) {
  const choose = page.locator(SELECTORS.chooseOrganization(organizationId));
  await expect.poll(async () => (await choose.count()) > 0 || (await currentOrganizationId(page)) === organizationId,
    { timeout: 25000 }).toBe(true);
  if (await choose.count()) await choose.click();
  await expect.poll(() => currentOrganizationId(page), { timeout: 25000 }).toBe(organizationId);
}

// What the page shows right now, so a failed control explains itself (private run output only).
async function describePage(page) {
  try {
    const text = await page.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').slice(0, 400));
    const me = await page.evaluate(async () => {
      const response = await fetch('/api/v0/account/browser/me', { credentials: 'same-origin' });
      return response.status + ' ' + (await response.text()).slice(0, 160);
    });
    const cookies = (await page.context().cookies()).map((cookie) => `${cookie.name}(${cookie.value.slice(0, 9)}..;${cookie.sameSite};${cookie.secure})`);
    return `url=${page.url()} me=[${me}] cookies=${JSON.stringify(cookies)} text="${text}"`;
  } catch (error) { return `unreadable page: ${error.message}`; }
}

// Sign in as `actor` (default the tenant's admin) and land in the tenant's organization.
async function openAsAccount(page, tenant, { actor = tenant.admin, route = 'agents' } = {}) {
  const seen = [];
  const record = (response) => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/v0/')) seen.push(`${Date.now() % 100000} ${response.request().method()} ${url.pathname} ${response.status()}`);
  };
  page.on('response', record);
  await signInWithGoogle(page, actor.email);
  try {
    await chooseOrganization(page, tenant.org_id);
  } catch (error) {
    throw new Error(`${error.message} :: ${await describePage(page)} :: responses=${JSON.stringify(seen.slice(-14))}`);
  } finally {
    page.off('response', record);
  }
  // The app plays its entry transition and then lands on Home; only set the route once the
  // shell exists, otherwise the transition resets the page.
  try {
    await expect(page.locator(SELECTORS.profileMenuToggle)).toBeVisible({ timeout: 40000 });
  } catch (error) {
    throw new Error(`shell not visible :: ${await describePage(page)}`);
  }
  // A person with several workspaces lands in the first one the app picks; select the intended one.
  await page.evaluate((workspace) => sessionStorage.setItem('weaves.workspace', workspace), tenant.workspace_id);
  await page.evaluate((hash) => { location.hash = hash; }, route);
  await page.reload();
  try {
    await expect(page.locator(SELECTORS.profileMenuToggle)).toBeVisible({ timeout: 40000 });
  } catch (error) {
    throw new Error(`shell not visible after reload :: ${await describePage(page)}`);
  }
  await page.waitForTimeout(1500);
  const shown = await describePage(page);
  if (process.env.TENANCY_BROWSER_DEBUG) console.log('opened as account', actor.email, shown);
}

async function switchOrganization(page, organizationId) {
  await page.evaluate((selector) => document.querySelector(selector).click(), SELECTORS.profileMenuToggle);
  await page.evaluate((selector) => document.querySelector(selector).click(), SELECTORS.switchOrganization(organizationId));
}

async function signOut(page) {
  await page.evaluate((selector) => document.querySelector(selector).click(), SELECTORS.signOut);
}

// Signed out means no session cookie and no account material in either storage.
async function expectSignedOut(page, context) {
  await expect.poll(async () => (await context.cookies()).some((cookie) => cookie.name === SESSION_COOKIE)).toBe(false);
  expect(await page.evaluate(() => [...Object.keys(sessionStorage), ...Object.keys(localStorage)]
    .filter((key) => /^weaves\.(account|organization|workspace)$/.test(key)))).toEqual([]);
}

// The frontend must hold no credential in script-readable storage; the cookie is HttpOnly.
async function expectNoTokenInStorage(page) {
  const dump = await page.evaluate(() => JSON.stringify([{ ...sessionStorage }, { ...localStorage }]));
  expect(dump).not.toMatch(/wvs_(acbr|acct)_/);
}

async function expectSessionCookie(context) {
  const cookie = (await context.cookies()).find((item) => item.name === SESSION_COOKIE);
  expect(cookie, 'account session cookie').toBeTruthy();
  expect([cookie.httpOnly, cookie.secure, cookie.sameSite]).toEqual([true, true, 'Strict']);
}

// TODO(frontend-onboarding): plants private-looking state through the existing SPA globals so a
// session change can prove it is torn down. Adapt to whatever the new frontend exposes.
async function plantPrivateState(page, sentinel) {
  await page.evaluate((value) => {
    state.agentBuilder = { name: value, instructions: value };
    state.skillVersionCache = { privateVersion: { content: value } };
    showDialog({ type: 'info', title: value, message: value, primaryLabel: 'Close' });
    window.__stale = api('/audit-events').then(() => 'settled', () => 'rejected');
  }, sentinel);
}

const appStateText = (page) => page.evaluate(() => document.documentElement.innerText + document.documentElement.innerHTML
  + (typeof state === 'undefined' ? '' : JSON.stringify(state)));

module.exports = {
  SELECTORS, SESSION_COOKIE, chooseGoogleIdentity, signInWithGoogle, chooseOrganization, openAsAccount,
  switchOrganization, signOut, expectSignedOut, expectNoTokenInStorage, expectSessionCookie,
  currentOrganizationId, plantPrivateState, appStateText, describePage,
};
