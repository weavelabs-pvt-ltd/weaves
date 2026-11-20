const base = require('./playwright.config.cjs');
module.exports = { ...base, testMatch: 'live.spec.cjs', timeout: 120000 };
