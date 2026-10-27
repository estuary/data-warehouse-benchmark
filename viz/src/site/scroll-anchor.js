/**
 * Keep what the reader is looking at in place while the page grows above it.
 *
 * Charts draw as their queries return, and each one lengthens the page (Compare
 * grows by about 4,000 px while it loads). Chrome and Firefox anchor the scroll
 * position to the content on screen (CSS scroll anchoring), so a chart drawing
 * above the reader goes unnoticed. Safari does not: each chart drawing above
 * pushes the content on screen down, two or three times while the page loads,
 * which reads as being kicked back up the page while scrolling down.
 *
 * Where the browser does not anchor, this does the same by hand: it remembers an
 * element near the middle of the window and where it sat on the page, and when
 * the page changes size it scrolls by however far that element moved. The
 * position is kept in page coordinates, so the reader's own scrolling (which
 * does not move the element on the page) is never undone.
 */

/** How far down the window the anchor is taken, as a share of its height: clear of the sticky bars. */
const ANCHOR_AT = 0.4;

/** True when the browser anchors scrolling itself and has not been told not to. */
function nativeAnchoring() {
  if (!globalThis.CSS?.supports?.('overflow-anchor', 'auto')) return false;
  return getComputedStyle(document.documentElement).overflowAnchor !== 'none';
}

/** The first element at the anchor point that is not inside a sticky or fixed bar. */
function pickAnchor() {
  const y = window.innerHeight * ANCHOR_AT;
  for (const el of document.elementsFromPoint(window.innerWidth / 2, y)) {
    if (el === document.documentElement || el === document.body) continue;
    let pinned = false;
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const pos = getComputedStyle(n).position;
      if (pos === 'sticky' || pos === 'fixed') { pinned = true; break; }
    }
    if (!pinned) return el;
  }
  return null;
}

/** Install once, for the life of the document (every page shares it). */
export function holdScrollOnGrowth() {
  if (nativeAnchoring() || !('ResizeObserver' in window)) return;
  // The anchor and its ancestors, each with where it sat on the page. A chart
  // that redraws replaces the elements inside it, so the anchor itself can be
  // gone by the time the page has grown; the nearest ancestor still on the
  // page (the chart's container, a card) then stands in for it.
  let chain = [];
  let queued = false;
  const pageY = el => el.getBoundingClientRect().top + window.scrollY;
  const remember = () => {
    queued = false;
    chain = [];
    // At the very top there is nothing to hold: new content should push down.
    if (window.scrollY <= 0) return;
    for (let el = pickAnchor(); el && el !== document.body; el = el.parentElement) chain.push([el, pageY(el)]);
  };
  addEventListener('scroll', () => {
    if (!queued) { queued = true; requestAnimationFrame(remember); }
  }, { passive: true });
  new ResizeObserver(() => {
    const held = chain.find(([el]) => el.isConnected);
    if (held) {
      const moved = pageY(held[0]) - held[1];
      if (Math.abs(moved) >= 1) window.scrollBy({ top: moved, behavior: 'instant' });
    }
    remember();
  }).observe(document.body);
}
