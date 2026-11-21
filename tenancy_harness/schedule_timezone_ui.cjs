/* T4-I04: the real schedule editor in Chromium against the real API, restricted PostgreSQL and worker. */
const {expect} = require('@playwright/test');
const account = require('./browser_account.cjs');

const API = '/api/v0';
const dialog = (page) => page.locator('weaves-dialog[presentation="modal"]');
const rowOf = (page, id) => page.locator('.wf-interval-row[data-schedule-id="' + id + '"]');

async function schedules(request, headers) {
  const response = await request.get(API + '/workflow-schedules', {headers});
  expect(response.status()).toBe(200);
  return response.json();
}

async function openZoneEditor(page, scheduleId) {
  await rowOf(page, scheduleId).locator('[data-wf-action="edit-schedule"]').click();
  await expect(dialog(page).locator('#wf-schedule-zone')).toBeVisible();
}

async function saveSchedule(page) {
  await dialog(page).getByRole('button', {name: 'Save schedule', exact: true}).click();
}

async function addSchedule(page, name, zone) {
  await page.locator('[data-wf-action="new-schedule"]').click();
  await expect(dialog(page).locator('#wf-schedule-zone')).toBeVisible();
  await dialog(page).locator('[name="wf-schedule-name"]').fill(name);
  await dialog(page).locator('[name="wf-schedule-task"]').fill('Timezone control task');
  await dialog(page).locator('#wf-schedule-zone').fill(zone);
  await saveSchedule(page);
}

async function verify({browser, request}, fixture) {
  const z = fixture.schedule_zones, tenant = z.tenant, headers = tenant.admin.headers;
  const workflowId = tenant.workflow.definition.workflow_id;
  const seededId = tenant.schedule.schedule_id;
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await account.openAsAccount(page, tenant, {route: 'workflows/' + workflowId + '/triggers'});
    // Each schedule is shown in its own zone; a legacy record reads as Asia/Kolkata.
    await expect(rowOf(page, seededId)).toContainText(z.zone, {timeout: 25000});
    await expect(rowOf(page, z.legacy_schedule_id)).toContainText('Asia/Kolkata');

    // Changing only the zone keeps the pending instant and sends the revision the page loaded.
    const before = (await schedules(request, headers)).find((item) => item.schedule_id === seededId);
    await openZoneEditor(page, seededId);
    await dialog(page).locator('#wf-schedule-zone').fill('Asia/Tokyo');
    await saveSchedule(page);
    await expect(rowOf(page, seededId)).toContainText('Asia/Tokyo', {timeout: 25000});
    const after = (await schedules(request, headers)).find((item) => item.schedule_id === seededId);
    expect(after.timezone).toBe('Asia/Tokyo');
    expect(after.next_run_at, 'pending instant is unchanged by a zone-only edit').toBe(before.next_run_at);

    // A new schedule can name its own zone.
    await addSchedule(page, tenant.marker + ' ui london', 'Europe/London');
    await expect.poll(async () => (await schedules(request, headers)).some(
      (item) => item.name === tenant.marker + ' ui london' && item.timezone === 'Europe/London'), {timeout: 25000}).toBe(true);

    // An unknown zone is refused by the server, shown inline, and creates nothing.
    const count = (await schedules(request, headers)).length;
    await addSchedule(page, tenant.marker + ' ui bad', 'Mars/Olympus');
    await expect(dialog(page).locator('[data-schedule-error]')).toBeVisible();
    expect((await schedules(request, headers)).length).toBe(count);
    await dialog(page).getByRole('button', {name: 'Cancel', exact: true}).click();
  } finally { await context.close(); }
}

module.exports = {verify};
