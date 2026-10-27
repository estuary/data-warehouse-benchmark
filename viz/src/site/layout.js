/**
 * The site chrome every page shares: the top navigation, the disclosure line
 * and the footer. Pages carry an empty `<div id="site-header">` and
 * `<footer id="site-footer">`; this fills them, so a link or a disclosure is
 * written once and cannot drift between pages.
 */
// The two firms behind the study, as the header shows them: Estuary's official
// colour-on-dark logo (estuary.dev/brand-guidelines) and CorrDyn's white logo.
// Inlined so the header never waits on, or misses, a separate request offline.
import ESTUARY_LOGO from '../brand/estuary-logo-color-on-dark.png?inline';
import CORRDYN_LOGO from '../brand/corrdyn-logo-white.svg?inline';
import { ENGINE_LABEL } from '../engines.js';
import { REPORT_LIVE, reportHref } from './report.js';

/** Page id -> file and navigation label, in navigation order. */
export const PAGES = [
  { id: 'index', href: 'index.html', label: 'Overview' },
  { id: 'fit', href: 'fit.html', label: 'Find your warehouse' },
  { id: 'engine', href: 'engine.html', label: 'Engines' },
  { id: 'compare', href: 'compare.html', label: 'Compare all results' },
  { id: 'queries', href: 'queries.html', label: 'Query explorer' },
  { id: 'method', href: 'method.html', label: 'Method and glossary' }
];

// The report link lives in its own module so the browser tests can read it
// without loading this one (which needs a document).
export { REPORT_LIVE, REPORT_URL, reportHref } from './report.js';
export const REPO_URL = 'https://github.com/estuary/data-warehouse-benchmark';
export const ISSUES_URL = `${REPO_URL}/issues`;

/** Link to one engine's page. */
export const engineHref = id => `engine.html#engine=${encodeURIComponent(id)}`;

/** The folder the pages are served from, e.g. `https://host/preview/`. */
export const SITE_ROOT = new URL('./', document.baseURI);

/**
 * The page a URL opens, or null for anything that is not one of the pages
 * (another origin, a download, a data file). The root and `index.html` are the
 * Overview, and a host with clean URLs serves `compare.html` at `compare` too.
 * @param {string} href
 * @returns {string|null} A page id from `PAGES`.
 */
export function pageFor(href) {
  const url = new URL(href, location.href);
  if (url.origin !== SITE_ROOT.origin || !url.pathname.startsWith(SITE_ROOT.pathname)) return null;
  const rest = url.pathname.slice(SITE_ROOT.pathname.length);
  if (rest === '') return 'index';
  const name = /^([a-z]+)(?:\.html)?$/.exec(rest)?.[1];
  return PAGES.some(p => p.id === name) ? name : null;
}

function header(active) {
  const links = PAGES.map(p => {
    const current = p.id === active ? ' aria-current="page"' : '';
    if (p.id !== 'engine') return `<li><a href="${p.href}" data-page="${p.id}"${current}>${p.label}</a></li>`;
    // Engines is a menu: one page per engine, all from one template.
    const items = Object.entries(ENGINE_LABEL)
      .map(([id, label]) => `<li><a href="${engineHref(id)}">${label}</a></li>`).join('');
    return `<li class="has-menu">
      <details>
        <summary data-page="${p.id}"${current}>${p.label}</summary>
        <ul class="menu">${items}</ul>
      </details>
    </li>`;
  }).join('');
  // The buyer's guide is the site's call to action: on every page, once it is
  // published, opposite the logos and title, with the navigation on the row
  // below (on a phone, beside the menu button).
  const cta = REPORT_LIVE
    ? `<a class="btn btn-primary site-cta" href="${reportHref('header')}">Read the buyer's guide</a>`
    : '';
  return `
    <div class="site-bar">
      <a class="site-name" href="index.html">
        <span class="site-logos">
          <img class="logo-estuary" src="${ESTUARY_LOGO}" alt="Estuary" width="128" height="22">
          <span class="logo-x" aria-hidden="true">×</span>
          <img class="logo-corrdyn" src="${CORRDYN_LOGO}" alt="CorrDyn" width="142" height="14">
        </span>
        <span class="site-title">2026 Cloud Data Warehouse Benchmark</span>
      </a>
      <button class="nav-toggle" type="button" aria-expanded="false" aria-controls="site-nav">Menu</button>
      <nav id="site-nav" aria-label="Site"><ul>${links}</ul></nav>
      ${cta}
    </div>`;
}

function footer() {
  return `
    <div class="site-footer-grid">
      <div>
        <h3>About this study</h3>
        <p>Run jointly by <a href="https://estuary.dev/">Estuary</a>, the data movement platform that loaded the
        same data into all seven warehouses, and <a href="https://www.corrdyn.com/">CorrDyn</a>, the data and AI
        consultancy that designed the benchmark, wrote the code, ran it and wrote the report. Estuary was not one of
        the systems measured or compared.</p>
        <p>Measured July to October 2026 at list prices. Engines change every quarter; read this as a
        snapshot of Q3 2026. Not an audited TPC result: the TPC-DS and DSB workloads are derived from TPC-DS, and the
        results are not comparable to published TPC-DS results.</p>
      </div>
      <div>
        <h3>Check our work</h3>
        <ul>
          <li><a href="method.html">How the benchmark was run, and its limits</a></li>
          <li><a href="method.html#downloads">Download the data behind every chart</a></li>
          <li><a href="${REPO_URL}">Code, queries and raw results on GitHub</a></li>
          <li><a href="${ISSUES_URL}">Challenge a result or report an issue</a></li>
        </ul>
      </div>
      <div>
        <h3>${REPORT_LIVE ? 'Read the report' : 'Reference'}</h3>
        <ul>
          ${REPORT_LIVE ? `<li><a href="${reportHref('footer')}">The buyer's guide and full report</a></li>` : ''}
          <li><a href="method.html#glossary">Glossary of every term used here</a></li>
        </ul>
        <p class="fineprint">Runs entirely in your browser. Nothing you do here is sent anywhere.</p>
      </div>
    </div>`;
}

/**
 * Fill the site header and footer and wire the phone menu toggle.
 * @param {string} active Page id of the current page.
 */
export function mountSite(active) {
  const head = document.getElementById('site-header');
  if (head) head.innerHTML = header(active);
  const foot = document.getElementById('site-footer');
  if (foot) foot.innerHTML = footer();

  const toggle = head?.querySelector('.nav-toggle');
  toggle?.addEventListener('click', () => {
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(open));
    head.classList.toggle('nav-open', open);
  });
  // The Engines menu is a <details>; close it on an outside click or Escape.
  const menu = head?.querySelector('.has-menu details');
  if (menu) {
    document.addEventListener('click', e => { if (!menu.contains(e.target)) menu.open = false; });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') menu.open = false; });
  }
}

/**
 * Mark the current page in the navigation after an in-place page switch
 * (src/site/router.js), and close the phone menu and the Engines menu, which
 * the link that started the switch was in.
 * @param {string} active Page id of the page now shown.
 */
export function setActivePage(active) {
  const head = document.getElementById('site-header');
  if (!head) return;
  for (const el of head.querySelectorAll('[data-page]')) {
    if (el.dataset.page === active) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  }
  head.classList.remove('nav-open');
  head.querySelector('.nav-toggle')?.setAttribute('aria-expanded', 'false');
  const menu = head.querySelector('.has-menu details');
  if (menu) menu.open = false;
}
