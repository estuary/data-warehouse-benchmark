// Browser tests for the built explorer. They run against `dist/` served by a
// plain static server, never against `vite dev`: the DuckDB asset paths under
// `base: './'`, the service worker and the self-hosted Parquet extension are
// production-only behaviour, and those are the things the tests exist to catch.
import { defineConfig } from '@playwright/test';

// VIZ_PORT / VIZ_OUT_DIR let two builds be tested side by side (the default is
// the one CI runs): `VIZ_OUT_DIR=/tmp/x VIZ_PORT=4180 npx playwright test`.
const PORT = Number(process.env.VIZ_PORT || 4173);
const DIST = process.env.VIZ_OUT_DIR || 'dist';

export default defineConfig({
  testDir: 'tests',
  timeout: 90_000,
  expect: { timeout: 30_000 },
  // Tests run in parallel, one worker per core (VIZ_WORKERS overrides it). Most
  // share one running page per worker and reach their page by a link
  // (tests/app.js), so the 36 MB wasm module starts about once per worker
  // rather than once per test; they wait on page state (tests/wait.js), never
  // a sleep. About 1.5 minutes on four cores.
  fullyParallel: true,
  workers: process.env.VIZ_WORKERS ? Number(process.env.VIZ_WORKERS) : '100%',
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['github']] : 'list',
  use: {
    baseURL: `http://localhost:${PORT}/`,
    browserName: 'chromium',
    viewport: { width: 1280, height: 900 }
  },
  webServer: {
    command: `npx serve -l ${PORT} --no-clipboard ${DIST}`,
    url: `http://localhost:${PORT}/`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000
  }
});
