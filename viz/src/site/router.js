/**
 * The six pages are one document. Starting DuckDB-WASM (compiling the 36 MB
 * module, loading the Parquet extension, reading the exports) takes about two
 * seconds, and a page's own queries take tens of milliseconds, so a link that
 * loaded a new document would pay those two seconds on every click. A link to
 * another page is instead answered here: fetch that page's HTML (from the
 * offline cache once it has one), take the current page down, put the new
 * page's content in its place and run its code against the database that is
 * already open.
 *
 * Every page is still its own HTML file with its own URL, so a direct visit, a
 * reload, a bookmark and a shared link all work; the address bar and the
 * back button follow each switch. Links that are not to another page (a hash
 * on the same page, a download, another site, a modified click) are left to
 * the browser. If a switch fails for any reason the browser is sent to the
 * page the ordinary way.
 *
 * A page module exports `mount({ signal })`. The signal aborts when the reader
 * leaves the page: a page ties anything that outlives its DOM (a window
 * listener, an observer, a timer) to it.
 */
import { coordinator, namedPlots } from '@uwdata/vgplot';
import { measurePlots } from '../plot-helpers.js';
import { rememberLinkedView } from './chart-tools.js';
import { holdScrollOnGrowth } from './scroll-anchor.js';
import { bindTerms, hideTerm } from './glossary.js';
import { SITE_ROOT, pageFor, setActivePage } from './layout.js';

/** Each page's code, loaded the first time the page is shown. */
const MODULES = {
  index: () => import('../pages/index.js'),
  fit: () => import('../pages/fit.js'),
  engine: () => import('../pages/engine.js'),
  compare: () => import('../pages/compare.js'),
  queries: () => import('../pages/queries.js'),
  method: () => import('../pages/method.js')
};

/** Body children that belong to the site, not to a page: they stay across a switch. */
const KEEP = new Set(['site-header', 'site-footer', 'term-tip']);

/** Animation frames in a row with no query running before a page counts as quiet. */
const QUIET_FRAMES = 3;
/** Longest wait for the old page's queries before it is taken down anyway, in ms. */
const QUIET_CAP_MS = 3000;
/** How long a restored scroll position follows the page as its charts draw, in ms. */
const RESTORE_MS = 5000;

/**
 * True when Mosaic has no query queued or running. `pendingResults` is public;
 * the queue of queries not yet sent is private to Mosaic's QueryManager
 * (@uwdata/mosaic-core 0.31), with no public way to ask whether it is empty.
 * The caret range on a 0.x version keeps it at 0.31, so it can change only with
 * a deliberate upgrade; if one renames it, this falls back to the running
 * queries alone, so a page switch waits a little less rather than failing.
 */
export function idle() {
  const m = coordinator().manager;
  return m.pendingResults.length === 0 && (m.queue?.isEmpty?.() ?? true);
}

/**
 * Resolve once the database has been idle for a few frames in a row: one
 * query's result can start the next, so a single idle reading is not enough.
 * Never waits longer than QUIET_CAP_MS, and a hidden tab (no frames) waits no
 * longer than that either.
 */
function quiet() {
  return new Promise(resolve => {
    let calm = 0;
    let finished = false;
    const finish = () => { finished = true; resolve(); };
    const cap = setTimeout(finish, QUIET_CAP_MS);
    const tick = () => {
      if (finished) return;
      calm = idle() ? calm + 1 : 0;
      if (calm >= QUIET_FRAMES) { clearTimeout(cap); finish(); } else requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

/** The page on screen: its id, the signal its code listens to, and its first render. */
let current = null;

/** Whether `id` is the page on screen and still live (the back button stops it early). */
const showing = id => current?.id === id && !current.controller.signal.aborted;

/** Scroll without the smooth scrolling style.css asks for: a new page opens where it opens. */
const jump = top => window.scrollTo({ top, behavior: 'instant' });

/**
 * Return to a scroll position the page may not be tall enough for yet: charts
 * draw after their queries return, and each one lengthens the page. Follow the
 * page as it grows until the position fits, the reader scrolls or RESTORE_MS
 * passes.
 * @param {number} top
 */
function restoreScroll(top) {
  const stop = new AbortController();
  const done = () => stop.abort();
  const fits = () => document.documentElement.scrollHeight - window.innerHeight >= top;
  const place = () => {
    jump(top);
    if (fits()) done();
  };
  for (const type of ['wheel', 'touchstart', 'keydown', 'pointerdown']) {
    addEventListener(type, done, { signal: stop.signal, passive: true });
  }
  const growth = new ResizeObserver(place);
  growth.observe(document.body);
  stop.signal.addEventListener('abort', () => growth.disconnect());
  setTimeout(done, RESTORE_MS);
  place();
}

/** Resolve after `ms`. */
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

/**
 * Wait until the page being left has rendered and its queries are done, so
 * nothing it still has in flight lands on the next page (or is cut off and
 * fails loudly). Bounded, so a page that never settles cannot hold the switch.
 */
const settle = page => Promise.race([page.done.catch(() => undefined), delay(QUIET_CAP_MS)]).then(quiet);

/** Keep the scroll position in this history entry, for the back button. */
function saveScroll() {
  history.replaceState({ ...history.state, scrollY: window.scrollY }, '');
}

/**
 * Load a page's code; the first page's is loaded while the database starts.
 * @param {string} id
 */
export const loadPage = id => MODULES[id]();

/** The scripts a document loads: their hashed names change with every build. */
const appScript = doc => [...doc.querySelectorAll('script[type="module"][src]')].map(s => s.getAttribute('src')).join(' ');

/**
 * Fetch and parse a page's HTML. Offline, the service worker answers from its
 * cache. A page from a newer build than the one running (a deploy since this
 * tab opened) is refused, so the caller loads it the ordinary way rather than
 * running this build's code on that build's page.
 */
async function fetchPage(id) {
  const res = await fetch(new URL(`${id}.html`, SITE_ROOT));
  if (!res.ok) throw new Error(`${id}.html: HTTP ${res.status}`);
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  if (appScript(doc) !== appScript(document)) throw new Error(`${id}.html is from another build`);
  return doc;
}

/** Replace the current page's content, title and body class with `doc`'s. */
function swapContent(doc) {
  document.title = doc.title;
  const description = doc.querySelector('meta[name="description"]')?.getAttribute('content');
  if (description != null) document.querySelector('meta[name="description"]')?.setAttribute('content', description);
  document.body.className = doc.body.className;
  const ours = el => !KEEP.has(el.id) && el.tagName !== 'SCRIPT';
  for (const el of [...document.body.children]) if (ours(el)) el.remove();
  const fresh = [...doc.body.children].filter(ours);
  const footer = document.getElementById('site-footer');
  if (footer) footer.before(...fresh);
  else document.body.append(...fresh);
}

/**
 * Take the current page down: stop its listeners, disconnect its charts and
 * inputs from Mosaic (the query cache stays, so a page seen before comes back
 * from it), forget its named plots (the next visit names new ones, and the
 * legends must find those) and close any open definition.
 */
function unmount() {
  if (!current) return;
  current.controller.abort();
  current = null;
  coordinator().clear({ clients: true, cache: false });
  namedPlots.clear();
  hideTerm();
  document.body.classList.remove('ready');
}

/**
 * Run a page's code on the content now in the document, then mark it ready.
 * @param {string} id
 * @param {{default: (ctx: {signal: AbortSignal}) => Promise<void>|void}} mod
 * @returns {Promise<void>} Settles when the page has rendered.
 */
async function mount(id, mod) {
  const controller = new AbortController();
  const scrollToLinked = rememberLinkedView();
  setActivePage(id);
  // Chart widths depend on the page's layout (only Compare has a side nav).
  measurePlots();
  const done = (async () => {
    await mod.default({ signal: controller.signal });
    bindTerms(document.body);
    document.getElementById('status')?.remove();
    document.body.classList.add('ready');
  })();
  current = { id, controller, done };
  try {
    await done;
  } catch (err) {
    // A page taken down before it finished may fail on its missing content;
    // only the page still on screen reports a failure.
    if (current?.done === done) throw err;
    return;
  }
  scrollToLinked();
}

/**
 * Show the first page, the one the browser loaded, and start answering links.
 * @param {string} id
 * @param {Promise<object>} code The page's module, from `loadPage`.
 */
export async function openFirst(id, code) {
  // The router restores scroll itself: the browser's own restore runs before
  // the page it belongs to is back on screen.
  history.scrollRestoration = 'manual';
  holdScrollOnGrowth();
  const y = history.state?.scrollY;
  addEventListener('pagehide', saveScroll);
  addEventListener('click', onClick);
  addEventListener('popstate', onPopState);
  await mount(id, await code);
  const how = performance.getEntriesByType('navigation')[0]?.type;
  if ((how === 'reload' || how === 'back_forward') && Number.isFinite(y)) restoreScroll(y);
}

/** Switches asked for, oldest first; only the latest one waiting is kept. */
let next = null;
let switching = false;

/**
 * Switch to another page. A switch asked for while one is running waits for
 * it; asking again before it starts replaces it.
 * @param {{href?: string}} request `href` to push a new history entry; none
 *   to show whatever the address bar now names (the back button).
 */
function go(request) {
  next = request;
  if (!switching) drain();
}

async function drain() {
  switching = true;
  while (next) {
    const request = next;
    next = null;
    try {
      await show(request);
    } catch (err) {
      console.warn('in-place page switch failed, loading the page instead:', err);
      // The address may already name the page (the back button, or a failure
      // after the switch pushed it), and assigning the same URL with a hash
      // only scrolls: reload it then.
      const url = new URL(request.href ?? location.href, location.href).href;
      if (url === location.href) location.reload();
      else location.assign(url);
      return;
    }
  }
  switching = false;
}

/** One switch: fetch, take the old page down, swap, mount, land. */
async function show({ href }) {
  const target = new URL(href ?? location.href, location.href);
  const id = pageFor(target.href);
  if (!id) throw new Error(`not a page: ${target.href}`);
  if (showing(id)) {
    // Already there (a second click while the first switch ran): only the
    // hash can differ, and that page's own listeners handle a hash.
    if (href && target.href !== location.href) location.assign(target.href);
    return;
  }
  const back = !href;
  const y = back ? history.state?.scrollY : undefined;
  const t0 = performance.now();
  const [doc, mod] = await Promise.all([fetchPage(id), MODULES[id](), current && settle(current)]);
  // Another switch was asked for while this one loaded: that one wins.
  if (next) return;
  unmount();
  if (!back) history.pushState(null, '', target.href);
  swapContent(doc);
  jump(0);
  await mount(id, mod);
  if (Number.isFinite(y)) restoreScroll(y);
  if (!back) focusHeading();
  console.info(`${id} ready in ${Math.round(performance.now() - t0)} ms (in place)`);
}

/** Move focus to the new page's heading, so a screen reader announces the switch. */
function focusHeading() {
  const h1 = document.querySelector('h1');
  if (!h1) return;
  if (!h1.hasAttribute('tabindex')) h1.tabIndex = -1;
  h1.focus({ preventScroll: true });
}

/** A plain click on a link to another page switches in place. */
function onClick(e) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target instanceof Element ? e.target.closest('a[href]') : null;
  if (!(a instanceof HTMLAnchorElement) || a.hasAttribute('download')) return;
  if (a.target && a.target !== '_self') return;
  const id = pageFor(a.href);
  // Same page: the hash is that page's state, and its own listeners handle it.
  if (!id || (!next && showing(id))) return;
  e.preventDefault();
  if (!switching) saveScroll();
  go({ href: a.href });
}

/** Back and forward: switch when the entry is another page's. */
function onPopState() {
  const id = pageFor(location.href);
  if (!id || (!next && showing(id))) return;
  // Stop the page being left now: the address already names the next page,
  // and its hashchange listener (Engines) would act on that address.
  current?.controller.abort();
  go({});
}
