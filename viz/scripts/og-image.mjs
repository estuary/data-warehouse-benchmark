/**
 * Render the link-preview card, public/og-image.png (1200 x 630), from the
 * header's logos, the site's typeface and Estuary's colours. Run it again
 * after changing either logo or the copy below; the build only copies the PNG.
 *
 *   node scripts/og-image.mjs
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright';
import { PREVIEW_IMAGE } from '../src/site/published.js';

const here = dirname(fileURLToPath(import.meta.url));
const viz = join(here, '..');
const dataUri = (file, type) => `data:${type};base64,${readFileSync(join(viz, file)).toString('base64')}`;
const font = pathToFileURL(join(viz, 'node_modules/@fontsource-variable/inter/files/inter-latin-wght-normal.woff2')).href;

const html = `<!doctype html>
<html><head><meta charset="utf-8"><style>
  @font-face { font-family: Inter; src: url("${font}") format("woff2"); font-weight: 100 900; }
  * { box-sizing: border-box; margin: 0; }
  body {
    width: ${PREVIEW_IMAGE.width}px; height: ${PREVIEW_IMAGE.height}px; padding: 64px 72px;
    display: flex; flex-direction: column; justify-content: space-between;
    background: #04192b; color: #fff; font-family: Inter, sans-serif;
    border-bottom: 10px solid #6ed5d6;
  }
  .logos { display: flex; align-items: center; gap: 22px; }
  .logos .estuary { height: 44px; }
  .logos .corrdyn { height: 28px; }
  .logos span { color: #a4b6f4; font-size: 26px; }
  .eyebrow { font-size: 24px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: #6ed5d6; margin-bottom: 18px; }
  h1 { font-size: 68px; line-height: 1.08; font-weight: 700; letter-spacing: -0.02em; max-width: 1000px; }
  p { margin-top: 22px; font-size: 30px; line-height: 1.35; color: #a4b6f4; max-width: 1000px; }
  .url { font-size: 24px; font-weight: 600; color: #fff; }
</style></head><body>
  <div class="logos">
    <img class="estuary" src="${dataUri('src/brand/estuary-logo-color-on-dark.png', 'image/png')}" alt="">
    <span>&times;</span>
    <img class="corrdyn" src="${dataUri('src/brand/corrdyn-logo-white.svg', 'image/svg+xml')}" alt="">
  </div>
  <div>
    <div class="eyebrow">Interactive results</div>
    <h1>The 2026 Cloud Data Warehouse Benchmark</h1>
    <p>Seven cloud warehouses, 244 queries, 1 GB to 1 TB of data, 1 to 100 sessions at once.</p>
  </div>
  <div class="url">warehouse-benchmark.estuary.dev</div>
</body></html>`;

const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: PREVIEW_IMAGE.width, height: PREVIEW_IMAGE.height } });
  await page.setContent(html, { waitUntil: 'load' });
  await page.evaluate(() => document.fonts.ready);
  const out = join(viz, 'public', PREVIEW_IMAGE.path);
  await page.screenshot({ path: out, type: 'png' });
  console.log(`wrote ${out}`);
} finally {
  await browser.close();
}
