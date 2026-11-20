const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const fixture = JSON.parse(fs.readFileSync(process.env.TENANCY_FIXTURE_FILE, 'utf8'));
const { a, b } = fixture.tenants;
const get = (request, resource, actor = a.admin) => request.get('/api/v0' + resource, { headers: actor.headers });
async function open(page, tenant, route = '#agents') {
  await page.addInitScript(({ token, workspace }) => {
    sessionStorage.setItem('weaves.session', token);
    sessionStorage.setItem('weaves.workspace', workspace);
  }, { token: tenant.admin.headers.Authorization.slice(7), workspace: tenant.workspace_id });
  await page.goto(process.env.WEAVES_E2E_APP_URL + '/' + route);
}

test('T0-I01 real PostgreSQL API rejects missing authentication', async ({ request }) => {
  const health = await request.get('/api/v0/health');
  expect(await health.json()).toEqual({ status: 'ok', storage: 'postgres' });
  expect((await request.get('/api/v0/agents')).status()).toBe(401);
});

test('T0-I02 browser signs in using a real SMTP email proof', async ({ page }) => {
  await page.goto('/#member-signin');
  await page.locator('#member-email').fill(a.user.email);
  await page.getByRole('button', { name: 'Send code', exact: true }).click();
  await expect(page.locator('#member-code-0')).toBeVisible();
  const filename = crypto.createHash('sha256').update(a.user.email).digest('hex') + '.json';
  const mail = JSON.parse(fs.readFileSync(path.join(path.dirname(process.env.TENANCY_FIXTURE_FILE), 'mail', filename), 'utf8'));
  expect(mail.code).toMatch(/^\d{6}$/);
  for (let i = 0; i < 6; i++) await page.locator('#member-code-' + i).fill(mail.code[i]);
  await page.locator('#member-code-form button[type=submit]').click();
  await expect.poll(() => page.evaluate(() => Boolean(sessionStorage.getItem('weaves.session')))).toBe(true);
});

test('T0-I03 collection reads are tenant scoped', async ({ request }) => {
  for (const resource of ['/agents', '/workflows', '/model-providers', '/model-profiles', '/plugin-installations', '/users', '/roles', '/role-bindings', '/workspaces', '/approval-policies', '/audit-events', '/skills', '/evaluation-suites', '/workflow-triggers', '/workflow-schedules']) {
    const response = await get(request, resource);
    expect(response.status(), resource).toBe(200);
    const data = JSON.stringify(await response.json());
    expect(data, resource).not.toContain(b.org_id);
    expect(data, resource).not.toContain(b.marker);
  }
  const response = await get(request, '/agents');
  expect(JSON.stringify(await response.json())).toContain(a.agent.id);
  expect(JSON.stringify(await response.json())).not.toContain(a.second_agent.id);
});

test('T0-I04 foreign known resource and version IDs are hidden', async ({ request }) => {
  const foreign = [
    '/agents/' + b.agent.id, '/workflows/' + b.workflow.definition.workflow_id,
    '/tasks/' + b.task.task_id, '/skills/versions/' + b.skill.version.skill_version_id,
    '/approval-policies/' + b.policy.approval_policy_id,
    '/evaluation-suites/tenancy-b',
  ];
  for (const resource of foreign) expect((await get(request, resource)).status(), resource).toBe(404);
  expect((await get(request, '/agents/' + a.agent.id)).status()).toBe(200);
  const spoof = await request.get('/api/v0/agents', { headers: { ...a.admin.headers, 'X-Workspace-ID': b.workspace_id } });
  expect(spoof.status()).toBe(403);
});

test('T0-I05 foreign nested agent references do not create workflows', async ({ request }) => {
  const before = await (await get(request, '/workflows')).json();
  const denied = await request.post('/api/v0/workflows', { headers: a.admin.headers, data: { name: 'Foreign reference probe', agent_id: b.agent.id } });
  expect(denied.status()).toBe(404);
  const after = await (await get(request, '/workflows')).json();
  expect(after).toEqual(before);
});

test('T0-I06 Builder and User permissions remain distinct', async ({ request }) => {
  expect((await get(request, '/users', a.user)).status()).toBe(403);
  expect((await get(request, '/agents', a.user)).status()).toBe(200);
  const model = await request.post('/api/v0/model-providers', { headers: a.builder.headers, data: {
    provider_type: 'openai', display_name: 'Denied', api_key: 'fixture-only-denied' } });
  expect(model.status()).toBe(403);
  const agent = await request.post('/api/v0/agents', { headers: a.builder.headers, data: {
    name: 'builder-created-probe', instructions: a.marker } });
  expect(agent.status()).toBe(201);
});

test('T0-I07 private skill ownership applies inside the same organization', async ({ request }) => {
  const resource = '/skills/versions/' + a.skill.version.skill_version_id;
  expect((await get(request, resource, a.user)).status()).toBe(200);
  expect((await get(request, resource, a.admin)).status()).toBe(404);
  expect((await get(request, resource, a.builder)).status()).toBe(404);
});

test('T0-I08 private tasks and transcripts require their owner', async ({ request }) => {
  const resource = '/tasks/' + a.private_task.task_id;
  expect((await get(request, resource, a.user)).status()).toBe(200);
  expect((await get(request, resource, a.admin)).status()).toBe(404);
  expect((await get(request, resource + '/messages', a.builder)).status()).toBe(404);
});

test('T0-I09 service credentials cannot select another tenant or workspace', async ({ request }) => {
  expect((await get(request, '/agents', a.service)).status()).toBe(200);
  for (const workspace of [b.workspace_id, a.second_workspace_id]) {
    const response = await request.get('/api/v0/agents', { headers: { ...a.service.headers, 'X-Workspace-ID': workspace } });
    expect(response.status()).toBe(403);
  }
});

test('T0-I10 actual frontend renders only the signed-in tenant catalogue', async ({ page, browser }) => {
  await open(page, a);
  await expect(page.getByText(a.agent.name, { exact: true }).first()).toBeVisible();
  await expect(page.getByText(b.agent.name, { exact: true })).toHaveCount(0);
  const context = await browser.newContext();
  const other = await context.newPage();
  try {
    await open(other, b);
    await expect(other.getByText(b.agent.name, { exact: true }).first()).toBeVisible();
    await expect(other.getByText(a.agent.name, { exact: true })).toHaveCount(0);
  } finally { await context.close(); }
});

test('T0-I11 independent native worker completes tenant-labelled transcripts', async ({ request }) => {
  for (const tenant of [a, b]) {
    await expect.poll(async () => {
      const response = await get(request, '/tasks/' + tenant.task.task_id, tenant.admin);
      return (await response.json()).latest_turn_status;
    }, { timeout: 30000 }).toBe('succeeded');
    const transcript = await get(request, '/tasks/' + tenant.task.task_id + '/messages', tenant.admin);
    const content = JSON.stringify(await transcript.json());
    expect(content).toContain(tenant.marker);
    expect(content).not.toContain(tenant === a ? b.marker : a.marker);
  }
});
