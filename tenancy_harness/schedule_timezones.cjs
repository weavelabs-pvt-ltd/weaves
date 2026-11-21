/* T4-I03: API-level schedule timezones against the real API, PostgreSQL and native worker. */
const {expect} = require('@playwright/test');

const API = '/api/v0';
const EVERY_DAY = [0, 1, 2, 3, 4, 5, 6];

// Independent of the backend module: scan real minutes and compare the zone's wall clock.
function nextWallInstant(zone, hour, minute, afterMs) {
  const format = new Intl.DateTimeFormat('en-GB', {timeZone: zone, hourCycle: 'h23', hour: '2-digit', minute: '2-digit'});
  const wall = (ms) => format.format(new Date(ms));
  const target = String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0');
  for (let ms = Math.floor(afterMs / 60000) * 60000 + 60000; ms < afterMs + 49 * 3600000; ms += 60000) {
    if (wall(ms) === target) return ms;
  }
  throw new Error('no wall-clock match');
}

function wallParts(zone, ms) {
  const parts = new Intl.DateTimeFormat('en-GB', {timeZone: zone, hourCycle: 'h23', hour: '2-digit', minute: '2-digit'}).formatToParts(new Date(ms));
  const get = (type) => Number(parts.find((part) => part.type === type).value);
  return {hour: get('hour'), minute: get('minute')};
}

const hhmm = ({hour, minute}) => String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0');

async function interval(request, z) {
  const headers = z.tenant.admin.headers;
  const list = await (await request.get(API + '/workflow-schedules', {headers})).json();
  const seeded = list.find((item) => item.schedule_id === z.tenant.schedule.schedule_id);
  expect(seeded.timezone, 'schedule inherits the organization zone').toBe(z.zone);
  expect(seeded.next_run_at.endsWith('Z'), 'UTC instant').toBe(true);
  expect(Date.parse(seeded.next_run_at) - Date.parse(seeded.created_at)).toBe(86400000);
  const legacy = list.find((item) => item.schedule_id === z.legacy_schedule_id);
  expect(legacy.timezone, 'legacy record reads as Asia/Kolkata').toBe('Asia/Kolkata');
  expect(Date.parse(legacy.next_run_at) - Date.parse(legacy.created_at)).toBe(86400000);
}

async function createAndEdit(request, z) {
  const headers = z.tenant.admin.headers;
  const url = API + '/workflows/' + z.tenant.workflow.definition.workflow_id + '/schedules';
  const body = {name: z.tenant.marker + ' london', task_instructions: 'tz', interval_seconds: 86400};
  const bad = await request.post(url, {headers, data: {...body, timezone: 'Mars/Olympus'}});
  expect(bad.status()).toBe(422);
  const created = await request.post(url, {headers, data: {...body, timezone: 'Europe/London'}});
  expect(created.status()).toBe(201);
  const item = await created.json();
  expect(item.timezone).toBe('Europe/London');
  const edit = (data) => request.patch(API + '/workflow-schedules/' + item.schedule_id, {headers, data});
  expect((await edit({timezone: z.zone})).status(), 'revision required').toBe(422);
  expect((await edit({timezone: z.zone, expected_updated_at: '2020-01-01T00:00:00Z'})).status()).toBe(409);
  const ok = await edit({timezone: z.zone, expected_updated_at: item.updated_at});
  expect(ok.status()).toBe(200);
  const changed = await ok.json();
  expect(changed.timezone).toBe(z.zone);
  expect(changed.next_run_at, 'pending instant is untouched').toBe(item.next_run_at);
}

async function calendar(request, z, id, zone, expectedLabel) {
  const headers = z.tenant.admin.headers;
  const url = API + '/workflows/' + id + '/studio/settings';
  const before = await (await request.get(url, {headers})).json();
  expect(before.timezone).toBe(expectedLabel);
  const target = wallParts(zone, Date.now() + 10 * 60000);
  const expected = nextWallInstant(zone, target.hour, target.minute, Date.now());
  const patch = await request.patch(url, {headers, data: {sched: true, calendar: {freq: 'daily', days: EVERY_DAY, time: hhmm(target)}}});
  expect(patch.status()).toBe(200);
  const saved = await patch.json();
  expect(saved.timezone).toBe(expectedLabel);
  expect(Date.parse(saved.next_run_at)).toBe(expected);
  const read = await (await request.get(url, {headers})).json();
  expect(Date.parse(read.next_runs[0])).toBe(expected);
  return read;
}

async function workerFiresOnce(request, z) {
  const headers = z.tenant.admin.headers;
  const url = API + '/workflows/' + z.tenant.workflow.definition.workflow_id + '/schedules';
  const created = await (await request.post(url, {headers, data: {name: z.tenant.marker + ' fires', task_instructions: 'fire', interval_seconds: 60, timezone: z.zone}})).json();
  const search = API + '/audit-events?action=workflow.schedule.dispatched&target_id=' + created.schedule_id;
  let events = [];
  for (let attempt = 0; attempt < 40 && events.length === 0; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    events = await (await request.get(search, {headers})).json();
  }
  expect(events.length, 'the worker dispatched the occurrence').toBe(1);
  await new Promise((resolve) => setTimeout(resolve, 8000));
  const later = await (await request.get(search, {headers})).json();
  expect(new Set(later.map((event) => event.metadata.occurrence_at)).size).toBe(later.length);
  expect(later.length, 'repeated worker polls never repeat an occurrence').toBe(1);
  expect(Date.parse(events[0].metadata.occurrence_at)).toBe(Date.parse(created.next_run_at));
  await request.patch(API + '/workflow-schedules/' + created.schedule_id, {headers, data: {status: 'disabled'}});
}

async function verify({request}, fixture) {
  const z = fixture.schedule_zones;
  await interval(request, z);
  await createAndEdit(request, z);
  const zoned = await calendar(request, z, z.default_calendar_id, z.zone, z.zone);
  expect(zoned.next_runs.every((value) => /-0[45]:00$/.test(value))).toBe(true);
  const legacy = await calendar(request, z, z.legacy_calendar_id, 'Asia/Kolkata', 'Asia/Kolkata');
  expect(legacy.next_runs.every((value) => value.endsWith('+05:30'))).toBe(true);
  await workerFiresOnce(request, z);
}

module.exports = {verify};
