const { defineConfig } = require('@playwright/test');
const fs = require('node:fs');
const fixture = process.env.TENANCY_FIXTURE_FILE;
const url = new URL(process.env.WEAVES_E2E_APP_URL || 'http://invalid');
if (!fixture || !fs.existsSync(fixture) || url.hostname !== '127.0.0.1' || !url.port) {
  throw new Error('Required tenancy tests need the private disposable fixture and loopback app');
}
module.exports = defineConfig({
  testDir: __dirname,
  testMatch: ['controls.spec.cjs', 'diagnostics.spec.cjs'],
  workers: 1,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  timeout: 45000,
  outputDir: process.env.TENANCY_BROWSER_OUTPUT,
  use: { baseURL: url.origin, browserName: 'chromium', headless: true,
    reducedMotion: 'reduce', trace: 'off', video: 'off', screenshot: 'off' },
});
