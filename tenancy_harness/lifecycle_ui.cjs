/* T4-I05: the real lifecycle screens in Chromium: chooser states, export, delete and cancel. */
const {expect} = require('@playwright/test');
const account = require('./browser_account.cjs');

const dialog = (page) => page.locator('weaves-dialog[presentation="modal"]');
const card = (page, orgId) => page.locator('.account-organization[data-organization-id="' + orgId + '"]');

async function lifecycleApi(page, orgId, path = '/organization/lifecycle') {
  return page.evaluate(async ({orgId, path}) => {
    const response = await fetch('/api/v0' + path, {credentials: 'same-origin', headers: {'X-Organization-Id': orgId}});
    return {status: response.status, body: await response.json().catch(() => null)};
  }, {orgId, path});
}

async function openSettings(page) {
  await page.locator('#profile-menu-toggle').click();
  await page.locator('#organization-settings-open').click();
  await expect(page.locator('#org-settings-name')).toBeVisible({timeout: 25000});
}

async function verify({browser}, fixture) {
  const f = fixture.lifecycle_ui;
  const context = await browser.newContext({acceptDownloads: true});
  try {
    const page = await context.newPage();
    await account.signInWithGoogle(page, f.owner_email);
    // The chooser tells the truth about each state: suspended is shown, explained and not enterable.
    await expect(page.getByRole('heading', {name: 'Choose an organization'})).toBeVisible({timeout: 40000});
    await expect(card(page, f.alpha.org_id)).toBeEnabled();
    await expect(card(page, f.gamma.org_id)).toBeDisabled();
    await expect(card(page, f.gamma.org_id)).toContainText('Suspended');
    await card(page, f.alpha.org_id).click();
    await expect.poll(() => account.currentOrganizationId(page), {timeout: 40000}).toBe(f.alpha.org_id);
    await expect(page.locator('#profile-menu-toggle')).toBeVisible({timeout: 40000});

    // Export downloads a JSON file named for the organization.
    await openSettings(page);
    const downloading = page.waitForEvent('download');
    await page.locator('[data-lifecycle-action="export-data"]').click();
    const download = await downloading;
    expect(download.suggestedFilename()).toBe('organization-export-' + f.alpha.org_id + '.json');

    // Deleting needs the exact name; then only the recovery routes remain.
    await page.locator('[data-lifecycle-action="delete-organization"]').click();
    await page.locator('#org-delete-reason').fill('T4-I05 control');
    await page.locator('#org-delete-confirmation').fill(f.alpha.name);
    await dialog(page).getByRole('button', {name: 'Delete organization', exact: true}).click();
    await expect(page.locator('h1')).toContainText('is scheduled for deletion', {timeout: 40000});
    const pending = await lifecycleApi(page, f.alpha.org_id);
    expect(pending.status).toBe(200);
    expect(pending.body.state).toBe('deletion_requested');
    expect((await lifecycleApi(page, f.alpha.org_id, '/agents')).status, 'other routes are refused while pending').toBe(403);

    // Cancel from the recovery screen returns the organization to service.
    await page.locator('[data-lifecycle-action="cancel"]').click();
    await dialog(page).getByRole('button', {name: 'Cancel deletion', exact: true}).click();
    await expect.poll(() => account.currentOrganizationId(page), {timeout: 40000}).toBe(f.alpha.org_id);
    const restored = await lifecycleApi(page, f.alpha.org_id);
    expect(restored.body.state).toBe('active');
    expect((await lifecycleApi(page, f.alpha.org_id, '/agents')).status).toBe(200);
    // The other active organization was never touched.
    expect((await lifecycleApi(page, f.beta.org_id)).body.state).toBe('active');
  } finally { await context.close(); }
}

module.exports = {verify};
