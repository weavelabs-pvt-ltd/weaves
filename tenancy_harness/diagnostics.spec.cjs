const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const fixture = JSON.parse(fs.readFileSync(process.env.TENANCY_FIXTURE_FILE, 'utf8'));
// These explicit baseline observations are NOT isolation guarantees or xfails.
// Phase 1 replaces each observation with a passing security regression control.
for (const id of ['T0-D02', 'T0-D03', 'T0-D05']) {
  test(id + ' known defect baseline observation', async () => {
    expect(fixture.diagnostics[id], 'Known behavior changed: review and replace its baseline').toBe(true);
  });
}
test('T0-D04 known private-state retention observation in the actual browser', async ({ page }) => {
  const tenant = fixture.tenants.a;
  await page.addInitScript(({ token, workspace }) => {
    sessionStorage.setItem('weaves.session', token);
    sessionStorage.setItem('weaves.workspace', workspace);
  }, { token: tenant.admin.headers.Authorization.slice(7), workspace: tenant.workspace_id });
  await page.goto('/#agents');
  await expect(page.getByText(tenant.agent.name, { exact: true }).first()).toBeVisible();
  const observed = await page.evaluate(() => {
    state.agentBuilder = { instructions: 'PRIVATE_DIAGNOSTIC_SENTINEL' };
    state.skillVersionCache = { privateVersion: { content: 'PRIVATE_DIAGNOSTIC_SENTINEL' } };
    expireSession();
    return state.agentBuilder?.instructions === 'PRIVATE_DIAGNOSTIC_SENTINEL'
      && Boolean(state.skillVersionCache.privateVersion);
  });
  expect(observed, 'Known behavior changed: review and replace its baseline').toBe(true);
});
