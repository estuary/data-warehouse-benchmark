/**
 * Observable Plot labels each mark's <g> with its mark type ("dot", "line",
 * "rule", "tip"). An aria-label is not allowed on a <g> without a role, and
 * those words tell a screen reader nothing, so as plots render the label
 * moves to `data-mark`, where the export code and the tests find marks.
 * Every chart's numbers are also in a table or a CSV download.
 */
const strip = root => {
  for (const g of root.querySelectorAll('svg g[aria-label]:not([role])')) {
    g.dataset.mark = g.getAttribute('aria-label');
    g.removeAttribute('aria-label');
  }
};

export function stripPlotMarkLabels() {
  strip(document);
  new MutationObserver(records => {
    for (const r of records) for (const n of r.addedNodes) if (n.nodeType === 1) strip(n.tagName === 'g' ? n.parentNode : n);
  }).observe(document.body, { childList: true, subtree: true });
}

/**
 * A wide chart or table that scrolls sideways (on a phone, mostly) has to be
 * reachable from the keyboard, where the arrow keys scroll it (WCAG 2.1.1).
 * While a scroll area overflows it joins the tab order as a region named for
 * its section; once it fits again it leaves. Charts render after the page,
 * and widths change with the window, so this re-checks on both.
 */
const SCROLL_AREAS = '.view > div, .table-details > div, .table-scroll, .figure > div';

const markScrollAreas = () => {
  // Regions need distinct names; a second area in one section is numbered.
  const named = new Map();
  const nameFor = el => {
    const heading = el.closest('section, .card')?.querySelector('h2')?.textContent.trim() || 'Chart';
    const n = (named.get(heading) ?? 0) + 1;
    named.set(heading, n);
    return `${heading}${n > 1 ? `, part ${n}` : ''} (scrolls sideways)`;
  };
  for (const el of document.querySelectorAll(SCROLL_AREAS)) {
    const overflows = el.scrollWidth > el.clientWidth + 1;
    if (overflows && (!el.hasAttribute('tabindex') || el.hasAttribute('data-scroll-area'))) {
      el.dataset.scrollArea = '';
      el.tabIndex = 0;
      el.setAttribute('role', 'region');
      el.setAttribute('aria-label', nameFor(el));
    } else if (!overflows && el.hasAttribute('data-scroll-area')) {
      for (const a of ['data-scroll-area', 'tabindex', 'role', 'aria-label']) el.removeAttribute(a);
    }
  }
};

export function focusableScrollAreas() {
  let frame = 0;
  const schedule = () => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(markScrollAreas);
  };
  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  addEventListener('resize', schedule);
  schedule();
}
