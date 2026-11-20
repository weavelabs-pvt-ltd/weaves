/* global showDialog, api, setSession, prepareHomeEntry, refresh */
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

test('T1-I01 custom connector metadata belongs only to its organization', async ({ request, page, browser }) => {
  for (const [own, foreign] of [[a, b], [b, a]]) {
    for (const endpoint of ['/plugin-descriptors', '/capabilities']) {
      const response = await get(request, endpoint, own.admin);
      expect(response.status()).toBe(200);
      const text = JSON.stringify(await response.json());
      expect(text).toContain(own.marker);
      expect(text).not.toContain(foreign.marker);
      expect(text).not.toContain(foreign.installation.plugin_id);
    }
    const descriptors = await (await get(request, '/plugin-descriptors', own.admin)).json();
    expect(descriptors.map(item => item.plugin_id)).toEqual(expect.arrayContaining(['github', 'slack', 'jira', own.installation.plugin_id]));
    const foreignPath = '/api/v0/plugin-installations/' + foreign.installation.plugin_installation_id;
    const before = await (await get(request, '/plugin-installations', foreign.admin)).json();
    const denied = await request.patch(foreignPath, { headers: own.admin.headers, data: { status: 'disabled' } });
    expect(denied.status()).toBe(404);
    expect((await request.post(foreignPath + '/healthcheck', { headers: own.admin.headers })).status()).toBe(404);
    expect(await (await get(request, '/plugin-installations', foreign.admin)).json()).toEqual(before);
    const health = await request.post('/api/v0/plugin-installations/' + own.installation.plugin_installation_id + '/healthcheck', { headers: own.admin.headers });
    expect(health.status()).toBe(200);
  }
  await open(page, a, '#connections');
  await expect(page.getByText(a.marker + ' connector', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(b.marker + ' connector', { exact: true })).toHaveCount(0);
  const context = await browser.newContext();
  try {
    const other = await context.newPage();
    await open(other, b, '#connections');
    await expect(other.getByText(b.marker + ' connector', { exact: true }).first()).toBeVisible();
    await expect(other.getByText(a.marker + ' connector', { exact: true })).toHaveCount(0);
  } finally { await context.close(); }
});

test('T1-I02 explicit ownership and quarantine survive migration and cleanup', async ({ request }) => {
  const migration = fixture.catalog_ownership;
  expect(migration.version).toBe(20);
  expect(migration.applied).toBe(true);
  expect(migration.legacy_scoped).toBe(true);
  expect(migration.foreign_ref_denied).toBe(true);
  for (const tenant of [a, b]) {
    const descriptors = await (await get(request, '/plugin-descriptors', tenant.admin)).json();
    expect(JSON.stringify(descriptors)).not.toContain('MIGRATION_QUARANTINE_SENTINEL');
    expect(descriptors.some(item => item.plugin_id === migration.new_installation.plugin_id)).toBe(tenant === a);
    const id = migration.conflict_inst_ids[tenant.label];
    const removed = await request.delete('/api/v0/plugin-installations/' + id, { headers: tenant.admin.headers });
    expect(removed.status()).toBe(204);
    for (const actor of [a.admin, b.admin]) {
      const result = await get(request, '/plugin-descriptors', actor);
      const text = JSON.stringify(await result.json());
      expect(text).not.toContain(migration.conflict_id);
      expect(text).not.toContain(migration.orphan_id);
    }
  }
  const path = '/api/v0/plugin-installations/' + migration.new_installation.plugin_installation_id + '/healthcheck';
  expect((await request.post(path, { headers: a.admin.headers })).status()).toBe(200);
  expect((await request.post(path, { headers: b.admin.headers })).status()).toBe(404);
  const removed = await request.delete('/api/v0/plugin-installations/' + migration.new_installation.plugin_installation_id, { headers: a.admin.headers });
  expect(removed.status()).toBe(204);
});


test('T1-I03 provider egress and tenant credentials are contained in API and worker', async () => {
  expect(fixture.model_egress.api_denials).toBe(true);
  expect(fixture.model_egress.tenant_credentials).toBe(true);
  expect(fixture.model_egress.worker_denied).toBe(true);
  expect(fixture.model_egress.zero_probe_requests).toBe(true);
});

test('T1-I04 suspended and archived tenants cannot start or continue work', async () => {
  expect(fixture.lifecycle_control.api_denied).toBe(true);
  expect(fixture.lifecycle_control.webhook_denied).toBe(true);
  expect(fixture.lifecycle_control.no_webhook_jobs).toBe(true);
  expect(fixture.lifecycle_control.worker_blocked).toBe(true);
});

test('T1-I06 the same email signs in to an explicit tenant with independent throttles', async () => {
  const control = fixture.diagnostics.sign_in;
  expect(control.shared_person_in_both).toBe(true);
  expect(control.unscoped_ambiguous_issues_no_code).toBe(true);
  expect(control.tenant_a_throttled).toBe(true);
  expect(control.tenant_b_unaffected).toBe(true);
});

// Signs out the builder (revoking only that session); admin/user sessions stay valid for later stages.
test('T1-I05 session change tears down private browser state and ignores stale responses', async ({ browser }) => {
  const sentinel = 'TEARDOWN_SENTINEL_' + Date.now();
  const token = (actor) => actor.headers.Authorization.slice(7);
  const context = await browser.newContext({ baseURL: process.env.WEAVES_E2E_APP_URL });
  const page = await context.newPage();
  await page.addInitScript(({ session, workspace, draftKey, sentinelValue }) => {
    if (sessionStorage.getItem('test.seeded')) return;
    sessionStorage.setItem('test.seeded', '1');
    sessionStorage.setItem('weaves.session', session);
    sessionStorage.setItem('weaves.workspace', workspace);
    localStorage.setItem(draftKey, JSON.stringify([{ id: 'legacy', name: sentinelValue }]));
  }, { session: token(a.builder), workspace: a.workspace_id, draftKey: 'weaves.agentDrafts.' + a.workspace_id, sentinelValue: sentinel });
  await page.goto(process.env.WEAVES_E2E_APP_URL + '/#agents');
  await expect(page.getByText(a.agent.name, { exact: true }).first()).toBeVisible();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route('**/api/v0/audit-events**', async (route) => {
    await gate;
    try { await route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"expired"}' }); } catch { /* aborted by the page */ }
  });
  await page.evaluate((value) => {
    state.agentBuilder = { name: value, instructions: value };
    state.skillVersionCache = { privateVersion: { content: value } };
    showDialog({ type: 'info', title: value, message: value, primaryLabel: 'Close' });
    window.__stale = api('/audit-events').then(() => 'settled', () => 'rejected');
  }, sentinel);
  await page.evaluate(() => document.querySelector('[data-action="logout"]').click());
  await expect.poll(() => page.evaluate(() => state.authToken)).toBe('');
  await page.evaluate((userToken) => { setSession(userToken); prepareHomeEntry(); refresh(); }, token(a.user));
  release();
  await expect.poll(() => page.evaluate(() => state.authToken)).toBe(token(a.user));
  const view = await page.evaluate(() => [document.documentElement.innerText + document.documentElement.innerHTML, JSON.stringify(state)]);
  expect(view[0]).not.toContain(sentinel);
  expect(view[1]).not.toContain(sentinel);
  await expect(page.locator('#modal-root weaves-dialog')).toHaveCount(0);
  const keys = await page.evaluate(() => Object.keys(localStorage));
  expect(keys).not.toContain('weaves.agentDrafts.' + a.workspace_id);
  expect(keys).toContain('weaves.quarantine.agentDrafts.' + a.workspace_id);
  const other = await browser.newContext({ baseURL: process.env.WEAVES_E2E_APP_URL });
  const otherPage = await other.newPage();
  await open(otherPage, b);
  const otherView = await otherPage.evaluate(() => document.documentElement.innerText + JSON.stringify(state));
  expect(otherView).not.toContain(sentinel);
  expect(otherView).not.toContain(a.agent.name);
  await context.close();
  await other.close();
});

test('T1-I07 account sign-in links one person to both organizations without tenant access', async () => {
  const control = fixture.diagnostics.account;
  for (const key of ['code_sent', 'signed_in', 'one_account_two_organizations',
    'account_token_denied_on_tenant_routes', 'tenant_token_denied_on_account_routes',
    'unknown_email_indistinguishable']) {
    expect(control[key], key).toBe(true);
  }
});

test('T1-I08 an account selects an organization per request and tenant tokens cannot', async () => {
  const control = fixture.diagnostics.account;
  for (const key of ['selects_each_organization', 'foreign_and_unknown_indistinguishable',
    'tenant_token_ignores_header', 'workspace_checked_inside_organization']) {
    expect(control[key], key).toBe(true);
  }
});

test('T1-I09 an account attaches a second email only with proof and never takes another account\'s', async () => {
  const control = fixture.diagnostics.account;
  for (const key of ['email_attached_after_proof', 'email_of_another_account_refused', 'refusal_changed_nothing']) {
    expect(control[key], key).toBe(true);
  }
});
