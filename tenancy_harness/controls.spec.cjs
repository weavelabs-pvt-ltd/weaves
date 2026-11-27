const { test, expect } = require('@playwright/test');
const fs = require('node:fs');
const browserAccount = require('./browser_account.cjs');
require('./browser_trace.cjs').install(test);
const fixture = JSON.parse(fs.readFileSync(process.env.TENANCY_FIXTURE_FILE, 'utf8'));
const { a, b } = fixture.tenants;
const get = (request, resource, actor = a.admin) => request.get('/api/v0' + resource, { headers: actor.headers });
// API-only local debugging (run.py --api-only) omits the parts that need the signed-in frontend.
const withBrowser = !process.env.TENANCY_API_ONLY;
// Sign in through the real Google redirect flow (fake provider) and choose the organization.
const open = (page, tenant, route = 'agents', actor = tenant.admin) =>
  browserAccount.openAsAccount(page, tenant, { actor, route: route.replace(/^#/, '') });

test('T0-I01 real PostgreSQL API rejects missing authentication', async ({ request }) => {
  const health = await request.get('/api/v0/health');
  expect(await health.json()).toEqual({ status: 'ok', storage: 'postgres' });
  expect((await request.get('/api/v0/agents')).status()).toBe(401);
});

test('T0-I03 collection reads are tenant scoped', async ({ request }) => {
  for (const resource of ['/agents', '/workflows', '/model-providers', '/model-profiles', '/plugin-installations', '/users', '/roles', '/role-bindings', '/workspaces', '/audit-events', '/skills', '/evaluation-suites', '/workflow-triggers', '/workflow-schedules']) {
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
    try {
      await expect(other.getByText(b.agent.name, { exact: true }).first()).toBeVisible();
    } catch {
      throw new Error('tenant B catalogue not shown :: ' + await browserAccount.describePage(other));
    }
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
  if (!withBrowser) return;
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

// Signs out the builder's account session (revoking only that session); the admin, user and
// shared-person sessions stay valid for later stages.
test('T1-I05 session change tears down private browser state and ignores stale responses', async ({ browser, request }) => {
  const sentinel = 'TEARDOWN_SENTINEL_' + Date.now();
  const context = await browser.newContext({ baseURL: process.env.WEAVES_E2E_APP_URL });
  await context.addInitScript(({ draftKey, sentinelValue }) => {
    if (sessionStorage.getItem('test.seeded')) return;
    sessionStorage.setItem('test.seeded', '1');
    localStorage.setItem(draftKey, JSON.stringify([{ id: 'legacy', name: sentinelValue }]));
  }, { draftKey: 'weaves.agentDrafts.' + a.workspace_id, sentinelValue: sentinel });
  const page = await context.newPage();
  await open(page, a, 'agents', a.builder);
  await browserAccount.expectSessionCookie(context);
  await browserAccount.expectNoTokenInStorage(page);
  await expect(page.getByText(a.agent.name, { exact: true }).first()).toBeVisible();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  await page.route('**/api/v0/audit-events**', async (route) => {
    await gate;
    try { await route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"expired"}' }); } catch { /* aborted by the page */ }
  });
  await browserAccount.plantPrivateState(page, sentinel);
  const browserSession = (await context.cookies()).find((cookie) => cookie.name === browserAccount.SESSION_COOKIE).value;
  const withBrowserSession = () => request.get('/api/v0/agents', {
    headers: { Cookie: browserAccount.SESSION_COOKIE + '=' + browserSession, 'X-Organization-Id': a.org_id, Origin: process.env.WEAVES_E2E_APP_URL },
  });
  expect((await withBrowserSession()).status()).toBe(200);
  await browserAccount.signOut(page);
  await browserAccount.expectSignedOut(page, context);
  // Sign-out revokes the server session, not just the browser's copy of it.
  await expect.poll(async () => (await withBrowserSession()).status()).toBeGreaterThanOrEqual(401);
  expect((await get(request, '/agents', a.user)).status()).toBe(200);
  // The outstanding response settles after sign-out; it must not repopulate the page.
  release();
  await page.waitForTimeout(500);
  expect(await browserAccount.appStateText(page)).not.toContain(sentinel);
  // The held-response interception only exists to prove a late response is ignored; the next
  // person's Home page loads audit events normally.
  await page.unroute('**/api/v0/audit-events**');
  // A different person signs in, in the same browser context, while the old drafts still exist.
  await open(page, a, 'agents', a.user);
  await browserAccount.expectSessionCookie(context);
  expect(await browserAccount.appStateText(page)).not.toContain(sentinel);
  await expect(page.locator('#modal-root weaves-dialog')).toHaveCount(0);
  const keys = await page.evaluate(() => Object.keys(localStorage));
  expect(keys).not.toContain('weaves.agentDrafts.' + a.workspace_id);
  expect(keys).toContain('weaves.quarantine.agentDrafts.' + a.workspace_id);
  const other = await browser.newContext({ baseURL: process.env.WEAVES_E2E_APP_URL });
  const otherPage = await other.newPage();
  await open(otherPage, b);
  const otherView = await browserAccount.appStateText(otherPage);
  expect(otherView).not.toContain(sentinel);
  expect(otherView).not.toContain(a.agent.name);
  await context.close();
  await other.close();
});

test('T1-I07 Google sign-in links one person to both organizations and refuses what it must', async () => {
  const control = fixture.diagnostics.account;
  for (const key of ['signed_in', 'one_account_two_organizations',
    'session_without_organization_denied_on_tenant_routes', 'tenant_credential_denied_on_account_routes',
    'csrf_enforced', 'email_outside_allowlist_not_allowed', 'email_outside_allowlist_creates_nothing',
    'unverified_google_email_refused', 'unverified_google_email_creates_nothing',
    'provider_denial_refused', 'provider_denial_creates_nothing']) {
    expect(control[key], key).toBe(true);
  }
});

test('T1-I08 an account selects an organization per request and service credentials cannot', async () => {
  const control = fixture.diagnostics.account;
  for (const key of ['selects_each_organization', 'foreign_and_unknown_indistinguishable',
    'tenant_token_ignores_header', 'workspace_checked_inside_organization']) {
    expect(control[key], key).toBe(true);
  }
});

test('T1-I09 removing an identity always keeps the last verified one', async () => {
  const control = fixture.diagnostics.account;
  for (const key of ['google_sign_in_adds_two_verified_identities', 'other_account_identity_not_removable',
    'identity_removed_while_another_verified_remains', 'last_verified_identity_kept', 'other_account_unchanged']) {
    expect(control[key], key).toBe(true);
  }
});

test('T1-I10 real browser signs in with Google, chooses and switches organizations', async ({ browser }) => {
  const email = a.shared_person.email;
  const context = await browser.newContext({ baseURL: process.env.WEAVES_E2E_APP_URL });
  const page = await context.newPage();
  const tenantCalls = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith('/api/v0/') && !url.pathname.startsWith('/api/v0/account/') && url.pathname !== '/api/v0/health') {
      tenantCalls.push({ path: url.pathname, organization: request.headers()['x-organization-id'] });
    }
  });
  // The real UI path: the sign-in page's "Continue with Google" button, not a direct navigation.
  await browserAccount.chooseGoogleIdentity(email);
  await page.goto(process.env.WEAVES_E2E_APP_URL + '/#account-signin');
  await browserAccount.SELECTORS.continueWithGoogle(page).click();
  await expect(browserAccount.SELECTORS.chooserHeading(page)).toBeVisible({ timeout: 25000 });
  await browserAccount.expectSessionCookie(context);
  await browserAccount.expectNoTokenInStorage(page);
  // The same person may belong to more organizations (for example one created through the account API).
  for (const org of [a.org_id, b.org_id]) {
    await expect(page.locator(browserAccount.SELECTORS.chooseOrganization(org))).toBeVisible();
  }
  await browserAccount.chooseOrganization(page, a.org_id);
  // Organization data can arrive while the designed entry transition is still running.
  // Navigate through the visible UI, as a person does, after that transition finishes.
  await expect(page.locator(browserAccount.SELECTORS.profileMenuToggle)).toBeVisible({timeout:25000});
  await expect(page.locator('.weaves-transition')).toBeHidden({timeout:15000});
  await page.locator('nav a[data-page="agents"]').click();
  await expect(page.getByText(a.agent.name, { exact: true }).first()).toBeVisible({ timeout: 25000 });
  await expect(page.getByText(b.agent.name, { exact: true })).toHaveCount(0);
  expect(tenantCalls.length).toBeGreaterThan(0);
  expect(new Set(tenantCalls.map((call) => call.organization))).toEqual(new Set([a.org_id]));

  const beforeSwitch = tenantCalls.length;
  await browserAccount.switchOrganization(page, b.org_id);
  await expect.poll(() => browserAccount.currentOrganizationId(page), { timeout: 25000 }).toBe(b.org_id);
  await page.locator('nav a[data-page="agents"]').click();
  await expect(page.getByText(b.agent.name, { exact: true }).first()).toBeVisible({ timeout: 25000 });
  await expect(page.getByText(a.agent.name, { exact: true })).toHaveCount(0);
  const html = await browserAccount.appStateText(page);
  expect(html).not.toContain(a.agent.name);
  await page.waitForTimeout(1500);
  const afterSwitch = tenantCalls.slice(beforeSwitch).filter((call) => call.organization === a.org_id);
  expect(afterSwitch.length).toBeLessThanOrEqual(1);
  expect(tenantCalls.at(-1).organization).toBe(b.org_id);
  await context.close();
});

test('T1-I11 an account creates an organization once, idempotently and privately', async () => {
  const control = fixture.diagnostics.account;
  for (const key of ['organization_created', 'replay_returns_same_organization', 'key_reuse_with_other_body_conflicts',
    'creator_can_select_it', 'another_account_is_refused', 'no_tenant_token_issued',
    'role_title_saved', 'creation_stores_slug_timezone_locale', 'membership_is_owner_with_label',
    'slug_check_reports_taken_and_reserved', 'domain_peer_sees_the_claim', 'domain_peer_cannot_create']) {
    expect(control[key], key).toBe(true);
  }
});

test('TW-01 the public waitlist joins, answers, resumes and erases through the real API without any account', async () => {
  const control = fixture.waitlist;
  for (const key of ['questions_served', 'join_creates_submission', 'answer_saved', 'answer_changed',
    'answer_cleared', 'resume_with_token_only', 'known_email_gets_a_new_submission', 'known_email_leaks_no_answers',
    'first_token_still_has_its_answers', 'no_cookie_set', 'invalid_token_is_401', 'invalid_email_is_422',
    'rate_limit_returns_429_with_retry_after', 'operator_list_matches_rows', 'operator_export_has_the_entry',
    'operator_stats_count_entries', 'other_text_is_asked_on_role_and_location',
    'other_without_text_is_422_and_stores_nothing', 'other_text_is_trimmed_and_resumes', 'operator_shows_other_text',
    'rows_are_global_and_hold_only_a_digest', 'operator_actions_are_audited',
    'no_account_or_session_created', 'operator_delete_erases_entry', 'erased_token_stops_working']) {
    expect(control[key], key).toBe(true);
  }
});

test('T5-I01 tenant limits admit exactly the cap atomically, refuse the rest with 429 and cost no model call', async () => {
  const control = fixture.limits;
  for (const key of ['operator_override_sets_the_cap', 'exactly_the_cap_is_admitted',
    'refusals_use_the_limit_exceeded_contract', 'queued_rows_never_exceed_the_cap', 'refusals_create_no_rows',
    'other_organization_is_unaffected', 'same_key_in_another_tenant_is_a_separate_admission',
    'replays_at_the_limit_return_the_existing_task', 'no_model_call_before_the_worker_runs',
    'admitted_work_completes_in_the_worker', 'one_model_call_per_admitted_task', 'finished_work_frees_capacity',
    'usage_view_matches_the_rows']) {
    expect(control[key], key).toBe(true);
  }
});

test('T5-I05 a request id flows from the API response to the worker log and run audit trail without secrets', async () => {
  const control = fixture.correlation;
  for (const key of ['response_echoes_the_request_id', 'turn_finished_in_the_worker', 'turn_stores_the_request_id',
    'run_audit_trail_has_the_request_id', 'request_id_never_on_another_tenant', 'malformed_inbound_ids_are_replaced',
    'metrics_served_to_direct_peers', 'metrics_hidden_behind_a_proxy', 'metrics_have_no_tenant_identifiers',
    'every_log_line_is_structured_json', 'api_line_carries_the_request_id', 'worker_line_carries_the_request_id',
    'correlated_lines_stay_in_one_tenant', 'no_prompt_or_credential_in_any_log_line']) {
    expect(control[key], key).toBe(true);
  }
});

test('T5-I02 a backlogged organization cannot starve another and never exceeds its running cap', async () => {
  const control = fixture.fair_claim;
  for (const key of ['backlog_is_queued', 'backlogged_org_runs_at_its_cap', 'other_org_starts_within_the_bound',
    'other_org_waits_less_than_one_slow_call', 'other_org_finishes', 'backlogged_org_never_exceeds_its_cap']) {
    expect(control[key], key + ' ' + JSON.stringify(control.measured)).toBe(true);
  }
});

test('T4-I01 organization settings and ownership handoff preserve other tenant access', async ({browser, request}) => {
  await require('./organization_administration.cjs').verify({browser, request}, fixture);
});

test('T4-I02 organization export, operator suspend/resume and owner-requested deletion', async () => {
  const control = fixture.organization_lifecycle;
  for (const key of ['owner_export_downloads', 'deletion_needs_exact_name', 'owner_requests_deletion',
    'pending_deletion_refuses_other_routes', 'owner_cancels_deletion', 'operator_suspends',
    'suspended_refuses_owner', 'other_organization_still_works', 'operator_resumes', 'purge_needs_exact_name',
    'operator_purge_now_deletes', 'organization_rows_gone', 'other_organization_intact', 'deleted_never_selectable',
    'deleted_cannot_resume', 'worker_keeps_running', 'deleted_organization_counts_toward_cap']) {
    expect(control[key], key).toBe(true);
  }
});

test('T4-I03 schedule timezones are explicit, UTC on the wire and fire once through the real stack', async ({request}) => {
  test.setTimeout(240000);
  await require('./schedule_timezones.cjs').verify({request}, fixture);
});

test('T4-I04 the schedule editor shows, edits and validates time zones through the real stack', async ({browser, request}) => {
  test.setTimeout(240000);
  await require('./schedule_timezone_ui.cjs').verify({browser, request}, fixture);
});

test('T4-I05 the lifecycle screens export, request deletion and cancel through the real stack', async ({browser}) => {
  test.setTimeout(240000);
  await require('./lifecycle_ui.cjs').verify({browser}, fixture);
});

test('T5-I04 Home and every list stay bounded on a large history and match the database', async ({browser}) => {
  test.setTimeout(240000);
  await require('./bounded_queries_ui.cjs').verify({browser}, fixture);
});

test('T5-I03 the usage ledger equals the run rows, is tenant-isolated and survives a killed worker', async () => {
  const control = fixture.usage_ledger;
  for (const key of ['ledger_row_per_succeeded_run_with_equal_tokens', 'tokens_are_exactly_what_the_provider_reported',
    'daily_rollup_equals_ledger', 'usage_endpoint_matches_own_ledger', 'usage_endpoint_is_scoped_to_the_organization',
    'member_is_forbidden_and_bad_queries_rejected', 'restricted_role_sees_only_its_own_usage',
    'replayed_reserve_is_a_noop', 'replayed_settle_never_double_counts', 'operator_report_matches_ledger',
    'operator_report_has_no_content', 'call_held_open_with_a_reservation',
    'killed_worker_reservation_becomes_unknown_once', 'unknown_call_counts_no_tokens',
    'unknown_call_is_reported_not_hidden', 'reconciler_does_not_repeat']) {
    expect(control[key], key).toBe(true);
  }
});

test('T6-I03 the kill switches take effect on the running stack and a quarantined tenant stops alone', async () => {
  const control = fixture.kill_switch;
  for (const key of ['command_ok', 'schedule_disabled', 'trigger_disabled', 'a_api_refused', 'a_webhook_refused',
    'a_no_job_created', 'b_still_serves_and_completes', 'b_rows_untouched', 'repeat_is_idempotent', 'release_ok',
    'a_api_restored', 'schedule_reenabled', 'trigger_reenabled', 'a_webhook_works_again', 'b_rows_untouched_after_release',
    'quarantine_and_release_are_audited', 'audit_rows_name_operator_reason_and_correlation', 'ticket_is_recorded',
    'other_tenant_has_no_audit_rows', 'audit_is_append_only', 'restricted_role_cannot_read_audit',
    'read_only_refuses_writes_with_a_clear_503', 'read_only_still_serves_reads', 'read_only_clears',
    'admissions_pause_refuses_new_work', 'admissions_pause_clears', 'worker_pause_leaves_work_queued',
    'worker_pause_clears_and_work_completes', 'switches_need_no_restart', 'clear_returns_to_environment_defaults',
    'switch_changes_are_audited', 'restricted_role_cannot_change_a_switch']) {
    expect(control[key], key).toBe(true);
  }
});

test('T7-I01 the Slack bot install binds one workspace to one organization and keeps the token in the secret store', async () => {
  const control = fixture.slack_install;
  for (const key of ['start_refused_without_a_session', 'start_returns_the_bot_scopes_only',
    'install_completes_and_redirects_ok', 'state_is_single_use', 'status_shows_workspace_installer_and_state',
    'token_is_only_in_the_secret_store', 'no_token_in_responses_tables_audit_or_ciphertext', 'install_is_audited',
    'second_organization_is_refused_with_a_clear_reason', 'refused_install_token_is_revoked',
    'workspace_stays_bound_to_one_organization', 'reinstall_updates_in_place_and_rotates_the_token',
    'enterprise_grid_org_wide_install_is_refused', 'rls_hides_the_installation_from_other_tenants_and_no_scope',
    'another_organization_cannot_disconnect_it', 'disconnect_revokes_and_deletes_the_token',
    'freed_workspace_can_be_claimed_by_another_organization', 'the_workspace_is_free_for_the_next_control',
    'no_token_leaked_after_the_whole_flow']) {
    expect(control[key], key).toBe(true);
  }
});

test('T7-I02 the signed Slack events endpoint dedupes, acks within three seconds and reacts after the ack', async () => {
  const control = fixture.slack_events;
  for (const key of ['url_verification_is_echoed', 'forged_signature_is_refused_and_stores_nothing',
    'stale_timestamp_is_refused_and_stores_nothing', 'unknown_team_is_acked_and_dropped',
    'mention_is_acked_within_three_seconds_and_stored_pending', 'eyes_reaction_is_added_after_the_ack',
    'a_repeated_event_id_is_stored_once', 'slack_retry_headers_are_deduplicated',
    'one_message_under_two_event_ids_is_one_row', 'parallel_deliveries_store_one_row_and_one_reaction',
    'a_failing_slack_api_does_not_fail_the_ack', 'a_slow_slack_api_does_not_delay_the_ack',
    'a_suspended_organization_drops_events_without_text', 'a_resumed_organization_accepts_events_again',
    'the_platform_pause_drops_events_without_text', 'the_platform_pause_clears_without_a_restart',
    'inbox_is_invisible_to_tenants_and_unscoped_roles',
    'revoking_only_user_tokens_leaves_the_bot_alone', 'bot_token_revocation_marks_the_installation_revoked',
    'a_revoked_workspace_reinstalls_in_place', 'app_uninstalled_ends_it_and_deletes_the_token',
    'events_after_uninstall_are_acked_and_dropped']) {
    expect(control[key], key).toBe(true);
  }
});

test('T7-I03 the Slack bot lifecycle ends, suspends and purges cleanly on the real stack', async () => {
  const control = fixture.slack_lifecycle;
  for (const key of ['app_uninstalled_ends_it_and_deletes_the_token',
    'pending_events_of_the_workspace_are_dropped_without_text',
    'the_other_tenants_installation_and_inbox_are_untouched',
    'events_for_an_ended_workspace_are_acked_and_unknown', 'a_repeated_uninstall_changes_nothing',
    'another_organization_installs_the_freed_workspace', 'the_new_owner_can_release_it_again',
    'a_clean_reinstall_follows_an_uninstall', 'revoking_only_user_tokens_leaves_the_bot_alone',
    'bot_token_revocation_ends_it_and_deletes_the_token', 'a_revoked_workspace_reinstalls_in_place',
    'quarantine_suspends_the_installation', 'a_quarantined_organizations_events_are_dropped_without_text',
    'quarantine_leaves_the_other_tenant_working', 'release_restores_the_suspended_installation',
    'quarantine_with_revocation_ends_the_token_for_good', 'the_admin_can_reinstall_after_a_quarantine',
    'organization_deletion_removes_the_slack_data', 'organization_deletion_revokes_the_bot_token_at_slack',
    'organization_deletion_leaves_the_other_tenant_as_it_was',
    'no_token_in_any_table_audit_or_ciphertext']) {
    expect(control[key], key).toBe(true);
  }
});

test('T7-I04 a Slack mention becomes one hidden task and one answer, with the working message, private refusals and cheap exits', async () => {
  const control = fixture.slack_ask;
  for (const key of ['the_mention_becomes_one_hidden_private_task',
    'the_task_is_owned_by_a_service_account_with_two_permissions', 'the_hidden_task_is_not_listed_for_the_admin',
    'eyes_reaction_then_one_new_answer_in_the_thread', 'the_answer_carries_the_evidence_and_timing_line',
    'the_egress_scrubber_and_sanitizer_hold', 'the_inbox_row_is_settled_without_its_text',
    'a_replayed_event_creates_no_second_task_or_answer', 'the_request_id_is_slack_event',
    'a_slow_answer_shows_one_working_message_edited_in_place', 'the_working_message_becomes_the_receipt',
    'never_more_than_two_posts_per_request', 'a_follow_up_mention_continues_the_same_conversation',
    'help_and_greeting_are_threaded_replies', 'status_is_private',
    'a_missing_pr_link_asks_for_a_re_mention_with_an_example', 'a_command_without_its_rule_is_refused_privately',
    'cheap_exits_create_no_task_and_count_nothing',
    'a_failed_plan_says_try_again_privately_and_posts_nothing_public',
    'the_ask_limit_is_told_privately_with_the_wait', 'a_paused_bot_says_it_is_unavailable',
    'no_model_provider_is_told_privately', 'thread_rows_are_invisible_to_other_tenants_and_unscoped_roles',
    'no_question_text_in_thread_rows_or_audit']) {
    expect(control[key], key).toBe(true);
  }
});

test('T7-I06 @Weaves task: creates a normal task as the mapped member, with the receipt, the result, Already tracked and private refusals', async () => {
  const control = fixture.slack_task;
  for (const key of ['the_task_is_a_normal_workspace_task_created_as_the_mapped_member',
    'it_is_listed_with_its_slack_origin_and_in_the_normal_list',
    'the_origin_has_a_slack_thread_link_and_no_slack_internals',
    'the_first_message_is_created_then_edited_into_the_receipt',
    'the_result_is_one_new_message_with_the_full_result_pointer', 'both_messages_link_to_the_task_in_weaves',
    'never_more_than_two_posts_per_request', 'the_egress_scrubber_and_sanitizer_hold',
    'the_inbox_row_is_settled_without_its_text', 'one_allowed_execute_record_per_request',
    'a_repeat_task_in_the_thread_is_already_tracked_with_view_task',
    'a_replayed_event_creates_no_second_task_or_post', 'an_empty_task_asks_for_the_text_and_creates_nothing',
    'a_rule_that_is_off_is_refused_privately_with_scene_e', 'no_default_agent_is_told_privately',
    'the_execute_limit_is_told_privately_with_the_wait', 'blocked_requests_create_no_task',
    'the_blocked_attempts_are_audited_without_text', 'an_unknown_default_agent_is_rejected_by_the_settings_api',
    'thread_rows_are_invisible_to_other_tenants_and_unscoped_roles', 'no_task_text_in_thread_rows_or_audit']) {
    expect(control[key], key).toBe(true);
  }
});

test('T7-I05 the asker thumbs on a Slack answer become one rating, the latest wins and nothing else counts', async () => {
  const control = fixture.slack_feedback;
  for (const key of ['the_askers_thumbs_up_on_our_answer_is_one_rating',
    'the_rating_joins_to_its_turn_task_and_request', 'the_rating_keeps_the_agent_model_and_connector_snapshot',
    'the_asker_is_a_keyed_hash_not_the_slack_id', 'a_later_thumbs_down_replaces_it_newest_wins',
    'a_redelivered_event_is_stored_once', 'another_users_reaction_changes_nothing',
    'a_reaction_on_a_message_that_is_not_our_answer_changes_nothing',
    'our_own_reaction_and_other_emoji_are_never_stored', 'ignored_reactions_leave_the_rating_as_it_was',
    'the_thumbsup_alias_with_a_skin_tone_counts_as_a_thumbs_up', 'reaction_inbox_rows_are_settled_without_text',
    'the_pepper_lives_in_the_secret_store_once', 'the_rating_is_invisible_to_other_tenants_and_unscoped_roles',
    'no_question_text_or_raw_slack_id_in_the_rating',
    'no_question_text_in_the_audit_trail_and_no_feedback_audit_noise']) {
    expect(control[key], key).toBe(true);
  }
});

test('T7-I07 Stop and Try again: signed presses, who may press, cancellation, no late answer, gates and audit', async () => {
  const control = fixture.slack_interactions;
  for (const key of ['a_signed_press_is_acked_within_three_seconds',
    'the_working_message_carries_a_stop_button_with_an_opaque_reference',
    'another_person_is_told_privately_and_nothing_changes', 'an_admin_holding_slack_manage_can_stop_it',
    'stop_turns_the_working_message_into_stopped_without_buttons',
    'the_turn_is_cancelled_and_no_late_answer_is_posted', 'a_forged_reference_or_message_finds_nothing',
    'stop_works_while_the_bot_is_paused', 'a_replayed_press_is_one_press', 'a_double_press_says_already_stopped',
    'no_late_answer_after_a_stopped_request', 'a_forged_or_stale_signature_is_refused_and_stores_nothing',
    'a_press_for_an_unknown_workspace_is_acked_and_dropped', 'a_failed_request_offers_try_again',
    'try_again_is_refused_by_the_gates_while_the_bot_is_paused', 'try_again_by_someone_else_is_refused_privately',
    'try_again_runs_the_stored_request_as_a_new_turn_and_answers_once',
    'the_failure_message_became_the_working_message_and_lost_its_button',
    'try_again_counts_against_the_ask_limits', 'the_old_try_again_reference_is_dead_after_the_retry',
    'every_press_is_audited_once_with_its_reason_and_no_text', 'the_press_rows_lose_their_reference_once_settled',
    'no_token_in_any_table_audit_or_ciphertext']) {
    expect(control[key], key).toBe(true);
  }
});
