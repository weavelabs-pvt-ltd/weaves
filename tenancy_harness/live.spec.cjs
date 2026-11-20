const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const fixture = JSON.parse(fs.readFileSync(process.env.TENANCY_FIXTURE_FILE, 'utf8'));
if (!fixture.live) throw new Error('Live test preparation and approval gate are required');
const tenant = fixture.tenants.a;
async function open(page, hash) {
  await page.addInitScript(({ token, workspace }) => {
    sessionStorage.setItem('weaves.session', token);
    sessionStorage.setItem('weaves.workspace', workspace);
  }, { token: tenant.admin.headers.Authorization.slice(7), workspace: tenant.workspace_id });
  await page.goto('/#' + hash);
}
test('LIVE-I01 real Anthropic response appears in its task transcript', async ({ page }) => {
  await open(page, 'tasks/' + fixture.live.task_id);
  await expect(page.locator('.task-transcript')).toContainText(fixture.live.marker);
  await expect(page.locator('.task-transcript')).toContainText('Succeeded');
});
test('LIVE-I02 browser approval releases the second live agent', async ({ page }) => {
  const failures = [];
  page.on('response', response => {
    if (response.status() >= 400) failures.push({ path: new URL(response.url()).pathname, status: response.status() });
  });
  await open(page, `workflows/${fixture.live.workflow_id}/run/${fixture.live.run_id}`);
  await expect(page.getByRole('button', { name: /Approve/ }), JSON.stringify(failures)).toBeVisible({ timeout: 15000 });
  await page.getByRole('button', { name: /Approve/ }).click();
  await expect(page.getByRole('heading', { name: 'Workflow finished', exact: true })).toBeVisible({ timeout: 90000 });
  await expect(page.locator('.wf-run-summary')).not.toBeEmpty();
});
test('LIVE-I03 another organization cannot inspect the live run', async ({ request }) => {
  const path = `/api/v0/workflows/${fixture.live.workflow_id}/runs/${fixture.live.run_id}`;
  const denied = await request.get(path, { headers: fixture.tenants.b.admin.headers });
  expect(denied.status()).toBe(404);
});
