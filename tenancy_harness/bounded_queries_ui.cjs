/* T5-I04: Home in Chromium on a seeded large history stays bounded and tells the database's truth. */
const {expect} = require('@playwright/test');
const account = require('./browser_account.cjs');

const digits = (text) => Number(String(text).replace(/[^0-9]/g, ''));

async function verify({browser}, fixture) {
  const control = fixture.bounded_queries;
  // The API half already ran against the real stack; every named check must hold.
  for (const [key, value] of Object.entries(control)) {
    if (key !== 'browser') expect(value, key).toBe(true);
  }
  const {email, org_id: orgId, workspace_id: workspaceId, truth} = control.browser;
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const calls = [];
    const listSizes = [];
    page.on('response', async (response) => {
      const url = new URL(response.url());
      if (!url.pathname.startsWith('/api/v0/') || url.pathname.startsWith('/api/v0/account/') || response.request().method() !== 'GET') return;
      calls.push({path: url.pathname, search: url.search});
      try {
        const body = await response.json();
        const items = Array.isArray(body) ? body : Array.isArray(body?.items) ? body.items : null;
        if (items) listSizes.push({path: url.pathname, count: items.length});
      } catch { /* not JSON */ }
    });
    await account.openAsAccount(page, {org_id: orgId, workspace_id: workspaceId}, {actor: {email}, route: 'overview'});
    // Measure one clean Home load of the 2,000-task workspace.
    calls.length = 0;
    listSizes.length = 0;
    await page.reload();
    await expect(page.locator('#home-page')).toBeVisible({timeout: 40000});
    await expect(page.locator('.home-metric').first()).toBeVisible({timeout: 40000});
    await page.waitForTimeout(7000);

    const tasks = calls.filter((call) => call.path === '/api/v0/tasks');
    expect(calls.length, 'Home makes a bounded number of reads').toBeLessThanOrEqual(60);
    expect(tasks.length, 'at most one recent-tasks read').toBeLessThanOrEqual(2);
    expect(tasks.some((call) => call.search.includes('cursor=')), 'no history traversal').toBe(false);
    expect(calls.some((call) => call.search.includes('include_execution')), 'no per-task execution scans').toBe(false);
    expect(calls.filter((call) => call.path === '/api/v0/overview').length).toBeGreaterThanOrEqual(1);
    expect(calls.filter((call) => call.path === '/api/v0/overview').length).toBeLessThanOrEqual(2);
    for (const list of listSizes) {
      expect(list.count, list.path + ' returns a bounded page').toBeLessThanOrEqual(100);
    }
    expect(Math.max(...listSizes.filter((list) => list.path === '/api/v0/tasks').map((list) => list.count))).toBeLessThanOrEqual(10);

    // Tile numbers are the database's, not a count of whatever the browser managed to load.
    const tiles = page.locator('.home-metric');
    await expect(tiles.nth(0)).toContainText('Tasks run');
    expect(digits(await tiles.nth(0).locator('strong').innerText())).toBe(truth.turns_5_days);
    const settled = truth.succeeded_5_days + truth.failed_5_days;
    await expect(tiles.nth(1).locator('strong')).toHaveText(((truth.succeeded_5_days / settled) * 100).toFixed(1) + '%');
    await expect(tiles.nth(1)).toContainText(truth.failed_5_days + ' failed');

    // An idle Home does not poll: nothing is queued or running, so no further reads arrive.
    const before = calls.length;
    await page.waitForTimeout(8000);
    expect(calls.length - before, 'idle Home stays quiet').toBe(0);
  } finally { await context.close(); }
}

module.exports = {verify};
