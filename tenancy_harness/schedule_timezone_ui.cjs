/* T4-I04: the real schedule editor in Chromium against the real API, restricted PostgreSQL and worker.
   Every step waits on UI state (dialog open and idle, dialog closed after a save), schedule names are
   unique per run, and assertions tolerate unrelated schedules. No sleeps. */
const {expect} = require('@playwright/test');
const account = require('./browser_account.cjs');

const API = '/api/v0';
const SLOW = 25000;
const dialog = (page) => page.locator('weaves-dialog[presentation="modal"]');
const rowOf = (page, id) => page.locator('.wf-interval-row[data-schedule-id="' + id + '"]');

async function schedules(request, headers) {
  const response = await request.get(API + '/workflow-schedules', {headers});
  expect(response.status()).toBe(200);
  return response.json();
}

// A repeated run in one stack starts from the seeded zone; a zone-only edit keeps next_run_at.
async function restoreSeededZone(request, headers, seededId, zone) {
  const current = (await schedules(request, headers)).find((item) => item.schedule_id === seededId);
  if (current.timezone === zone) return;
  const response = await request.patch(API + '/workflow-schedules/' + seededId, {
    headers, data: {timezone: zone, expected_updated_at: current.updated_at}});
  expect(response.status()).toBe(200);
}

// The editor is interactive once exactly one dialog is open, idle, and its zone field is visible.
async function dialogReady(page) {
  await expect(dialog(page)).toHaveCount(1, {timeout: SLOW});
  await expect(dialog(page).locator('#wf-schedule-zone')).toBeVisible({timeout: SLOW});
  await expect(dialog(page)).not.toHaveAttribute('busy', /.*/, {timeout: SLOW});
}

// After a save the page reloads its data and then closes the dialog; wait for that to finish.
async function dialogClosed(page) {
  await expect(dialog(page)).toHaveCount(0, {timeout: SLOW});
}

async function fillField(page, selector, value) {
  const field = dialog(page).locator(selector);
  await field.fill(value);
  await expect(field).toHaveValue(value);
}

async function saveSchedule(page) {
  await dialog(page).getByRole('button', {name: 'Save schedule', exact: true}).click({timeout: SLOW});
}

async function openZoneEditor(page, scheduleId) {
  await rowOf(page, scheduleId).locator('[data-wf-action="edit-schedule"]').click({timeout: SLOW});
  await dialogReady(page);
}

async function addSchedule(page, name, zone) {
  await page.locator('[data-wf-action="new-schedule"]').click({timeout: SLOW});
  await dialogReady(page);
  await fillField(page, '[name="wf-schedule-name"]', name);
  await fillField(page, '[name="wf-schedule-task"]', 'Timezone control task');
  // The organization-zone prefill only fills an empty field; assert our value survives it.
  await fillField(page, '#wf-schedule-zone', zone);
  await saveSchedule(page);
}

async function named(request, headers, name) {
  return (await schedules(request, headers)).filter((item) => item.name === name);
}

async function verify({browser, request}, fixture) {
  const run = Math.random().toString(36).slice(2, 8), z = fixture.schedule_zones, tenant = z.tenant;
  const headers = tenant.admin.headers, workflowId = tenant.workflow.definition.workflow_id;
  const seededId = tenant.schedule.schedule_id;
  const london = tenant.marker + ' ui london ' + run, bad = tenant.marker + ' ui bad ' + run;
  await restoreSeededZone(request, headers, seededId, z.zone);
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await account.openAsAccount(page, tenant, {route: 'workflows/' + workflowId + '/triggers'});
    // Each schedule is shown in its own zone; a legacy record reads as Asia/Kolkata.
    await expect(rowOf(page, seededId)).toContainText(z.zone, {timeout: SLOW});
    await expect(rowOf(page, z.legacy_schedule_id)).toContainText('Asia/Kolkata', {timeout: SLOW});

    // Changing only the zone keeps the pending instant and sends the revision the page loaded.
    const before = (await schedules(request, headers)).find((item) => item.schedule_id === seededId);
    await openZoneEditor(page, seededId);
    await fillField(page, '#wf-schedule-zone', 'Asia/Tokyo');
    await saveSchedule(page);
    await dialogClosed(page);
    await expect(rowOf(page, seededId)).toContainText('Asia/Tokyo', {timeout: SLOW});
    const after = (await schedules(request, headers)).find((item) => item.schedule_id === seededId);
    expect(after.timezone).toBe('Asia/Tokyo');
    expect(after.next_run_at, 'pending instant is unchanged by a zone-only edit').toBe(before.next_run_at);

    // A new schedule can name its own zone.
    await addSchedule(page, london, 'Europe/London');
    await dialogClosed(page);
    await expect.poll(async () => (await named(request, headers, london)).map((item) => item.timezone),
      {timeout: SLOW}).toEqual(['Europe/London']);

    // An unknown zone is refused by the server, shown inline, and creates nothing.
    await addSchedule(page, bad, 'Mars/Olympus');
    await expect(dialog(page).locator('[data-schedule-error]')).toBeVisible({timeout: SLOW});
    expect(await named(request, headers, bad), 'the refused schedule must not exist').toEqual([]);
    await dialog(page).getByRole('button', {name: 'Cancel', exact: true}).click({timeout: SLOW});
    await dialogClosed(page);
  } finally { await context.close(); }
}

module.exports = {verify};
