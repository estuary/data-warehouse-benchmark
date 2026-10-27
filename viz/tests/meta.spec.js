/**
 * What other sites see of a page before any script runs: its canonical
 * address and its link preview, added at build by vite.config.js from the
 * page's own title and description, the preview card itself, and what search
 * engines may index.
 */
import { expect, test } from '@playwright/test';
import { PREVIEW_IMAGE, SITE_URL, pageUrl } from '../src/site/published.js';

const PAGES = ['index', 'fit', 'engine', 'compare', 'queries', 'method'];

/** HTML character references back to text: Vite writes `'` as `&#39;` in attributes. */
const decode = s => s?.replace(/&#39;/g, "'").replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

/** The content of `<meta {key}="{name}">` or the href of `<link rel="{name}">`, as text. */
const attr = (html, name) => {
  const meta = new RegExp(`<meta (?:property|name)="${name}" content="([^"]*)"`).exec(html);
  if (meta) return decode(meta[1]);
  return decode(new RegExp(`<link rel="${name}" href="([^"]*)"`).exec(html)?.[1]);
};

for (const page of PAGES) {
  test(`${page}.html: canonical address and link preview name the published site`, async ({ request }) => {
    const html = await (await request.get(`${page}.html`)).text();
    expect(attr(html, 'canonical')).toBe(pageUrl(page));
    expect(attr(html, 'og:url')).toBe(pageUrl(page));
    expect(attr(html, 'og:title')).toBe(decode(/<title>([^<]+)<\/title>/.exec(html)[1]));
    expect(attr(html, 'og:description')).toBe(attr(html, 'description'));
    expect(attr(html, 'og:image')).toBe(new URL(PREVIEW_IMAGE.path, SITE_URL).href);
    expect(attr(html, 'twitter:card')).toBe('summary_large_image');
  });
}

test('search engines may index the pages, but not the review previews', async ({ request }) => {
  for (const page of PAGES) {
    const html = await (await request.get(`${page}.html`)).text();
    expect(html, `${page}.html`).not.toMatch(/<meta name="robots"/);
  }
  const robots = await (await request.get('robots.txt')).text();
  expect(robots).toMatch(/^User-agent: \*$/m);
  expect(robots).toMatch(/^Disallow: \/preview\/$/m);
});

test('the preview card ships with the build at the size the tags declare', async ({ request }) => {
  const res = await request.get(PREVIEW_IMAGE.path);
  expect(res.status()).toBe(200);
  const png = await res.body();
  // A PNG's IHDR chunk holds its width and height at bytes 16 and 20.
  expect([png.readUInt32BE(16), png.readUInt32BE(20)]).toEqual([PREVIEW_IMAGE.width, PREVIEW_IMAGE.height]);
});
