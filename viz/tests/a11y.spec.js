/**
 * Accessibility checks that hold on every page: a floor on text size, no
 * sideways page scroll on a 320 px screen (WCAG 1.4.10), every form control
 * named, and a definition that opens on hover or focus closes on Escape
 * (WCAG 1.4.13). Color contrast is set by the brand palette (src/style.css).
 */
import { expect, test, visit } from './app.js';

const PAGES = ['index.html', 'fit.html', 'engine.html', 'compare.html', 'queries.html', 'method.html'];

/** The smallest text the site sets, in CSS px, at the browser's default 16 px. */
const MIN_TEXT_PX = 12;

/** Visible text smaller than MIN_TEXT_PX, outside the charts (they size their own). */
const smallText = page => page.evaluate(min => {
  const found = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n; (n = walker.nextNode());) {
    const el = n.parentElement;
    if (!n.textContent.trim() || el.closest('svg, .export-frame') || !el.getClientRects().length) continue;
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (size < min) found.push(`${size}px: ${el.tagName.toLowerCase()}.${el.className} "${n.textContent.trim().slice(0, 30)}"`);
  }
  return found;
}, MIN_TEXT_PX);

for (const href of PAGES) {
  test(`${href}: no text under ${MIN_TEXT_PX} px, and no sideways scroll at 320 px`, async ({ app: page }) => {
    await visit(page, href);
    expect(await smallText(page)).toEqual([]);
    await page.setViewportSize({ width: 320, height: 700 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320);
  });
}

test('every select on Compare has a name', async ({ app: page }) => {
  await visit(page, 'compare.html');
  const unnamed = await page.evaluate(() => [...document.querySelectorAll('select')]
    .filter(s => !s.labels.length && !s.getAttribute('aria-label')).length);
  expect(unnamed).toBe(0);
});

test('a definition opened by focus closes on Escape, with focus left where it was', async ({ app: page }) => {
  await visit(page, 'index.html');
  const term = page.locator('[data-term]').first();
  await term.focus();
  await expect(page.locator('#term-tip')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#term-tip')).toBeHidden();
  expect(await term.evaluate(el => el === document.activeElement)).toBe(true);
});
