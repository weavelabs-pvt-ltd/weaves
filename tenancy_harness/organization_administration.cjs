/* The actual frontend, Google cookie flow, API, restricted PostgreSQL and native worker. */
const {expect} = require('@playwright/test');
const account = require('./browser_account.cjs');

const dialog = page => page.locator('weaves-dialog[presentation="modal"]');
async function settings(page) {
  await page.locator('#profile-menu-toggle').click();
  await page.locator('#organization-settings-open').click();
  await expect(page.locator('#org-settings-name')).toBeVisible();
}
async function transfer(page, principal, name) {
  await dialog(page).getByRole('button', {name:'Transfer ownership', exact:true}).click();
  await page.locator('#org-new-owner').selectOption(principal);
  await page.locator('#org-transfer-confirmation').fill(name);
  await dialog(page).locator('[data-wd-primary]').click();
  await expect(page.locator('#org-settings-name')).toBeVisible();
}

async function verify({browser, request}, fixture) {
  const {primary, candidate, other} = fixture.org_administration;
  const context = await browser.newContext(), nextContext = await browser.newContext();
  try {
    const page = await context.newPage();
    await account.openAsAccount(page, primary, {route:'access'});
    await settings(page);
    const renamed = primary.marker + ' renamed';
    await page.locator('#org-settings-name').fill(renamed);
    await page.locator('#org-settings-timezone').fill('America/New_York');
    await page.locator('#org-settings-locale').fill('en-US');
    await dialog(page).getByRole('button', {name:'Save changes', exact:true}).click();
    await expect(dialog(page)).toHaveCount(0);
    await expect(page.locator('#crumb-workspace')).toHaveText(renamed);
    await settings(page);
    await transfer(page, candidate.principal_id, renamed);
    await expect(dialog(page).getByRole('button', {name:'Transfer ownership', exact:true})).toHaveCount(0);
    // The new owner's other membership is still a User, never an organization Admin.
    expect((await request.get('/api/v0/organization/settings', {headers:other.headers})).status()).toBe(403);
    const nextPage = await nextContext.newPage();
    await account.openAsAccount(nextPage, primary, {actor:candidate, route:'access'});
    await settings(nextPage);
    await transfer(nextPage, primary.admin.principal_id, renamed);
    await dialog(nextPage).getByRole('button', {name:'Cancel', exact:true}).click();
    await dialog(page).getByRole('button', {name:'Cancel', exact:true}).click();
    // Remove the former owner through People & access; only this membership is revoked.
    const members = await (await request.get('/api/v0/users', {headers:primary.admin.headers})).json();
    const member = members.find(item => item.principal_id === candidate.principal_id);
    expect(member).toBeTruthy();
    await page.locator(`[data-people-action="open"][data-id="${member.id}"]`).click();
    await page.getByRole('button', {name:'Remove from organization', exact:true}).click();
    await dialog(page).getByRole('button', {name:'Remove member', exact:true}).click();
    await expect(page.locator(`[data-people-action="open"][data-id="${member.id}"]`)).toHaveCount(0);
    expect((await request.get('/api/v0/auth/me', {headers:candidate.headers})).status()).toBe(403);
    expect((await request.get('/api/v0/auth/me', {headers:other.headers})).status()).toBe(200);
    expect((await request.get('/api/v0/account/browser/me', {headers:candidate.headers})).status()).toBe(200);
    await account.expectNoTokenInStorage(page);
    await account.expectNoTokenInStorage(nextPage);
  } finally { await context.close(); await nextContext.close(); }
}

module.exports = {verify};
