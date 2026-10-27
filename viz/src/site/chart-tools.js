/**
 * Download and share tools for every chart and table card.
 *
 * The explorer exists partly so that people can lift a chart into a deck or a
 * blog post. A screenshot of the live page loses the context a reader needs:
 * which data volume, which latency target, which engines, and where the
 * numbers came from. So every export is rebuilt as a self-describing figure:
 *
 *   title                         (the card's own heading)
 *   the current selection         (data volume, latency target, engines, ...)
 *   the chart and its legends     (cloned from the page, tooltips excluded)
 *   source line and link          (study, dates, the URL that reproduces it)
 *
 * The CSV is the rows behind the chart, queried from DuckDB with the same
 * filters, so a figure and its data can be cited together.
 */
import { toPng, toSvg } from 'html-to-image';
import { coordinator } from '@uwdata/vgplot';

/** Attribution printed under every exported figure. */
export const SOURCE_LINE =
  'Source: 2026 Cloud Data Warehouse Benchmark (Estuary and CorrDyn). Measured July to October 2026 at list prices.';

/** Export resolution multiplier: sharp on a slide at full width. */
const PIXEL_RATIO = 2;

const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/** Rows to CSV, quoting only where needed. */
export function toCsv(rows) {
  if (!rows.length) return '';
  const cols = Object.keys(rows[0]);
  const cell = v => {
    if (v == null) return '';
    const s = v instanceof Date ? v.toISOString() : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [cols.join(','), ...rows.map(r => cols.map(c => cell(r[c])).join(','))].join('\n');
}

function download(name, href) {
  const a = document.createElement('a');
  a.href = href;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
}

/** A shareable link to this card: the page URL (with its controls) plus the card. */
export function cardLink(id) {
  const url = new URL(location.href);
  const params = new URLSearchParams(url.hash.replace(/^#/, ''));
  params.set('view', id);
  url.hash = params.toString();
  return url.href;
}

/**
 * Build the off-screen figure an image export renders from.
 * @returns {HTMLElement} Attached to the document; the caller removes it.
 */
function exportFrame({ title, subtitle, node, link }) {
  const frame = document.createElement('div');
  frame.className = 'export-frame';
  const width = Math.max(node.scrollWidth, node.getBoundingClientRect().width, 480);
  frame.style.width = `${Math.ceil(width) + 48}px`;
  frame.innerHTML = `
    <p class="export-title">${esc(title)}</p>
    ${subtitle ? `<p class="export-sub">${esc(subtitle)}</p>` : ''}
    <div class="export-body"></div>
    <p class="export-source">${esc(SOURCE_LINE)}<br>${esc(link)}</p>`;
  const clone = node.cloneNode(true);
  // Tooltips, toolbars and anything interactive-only stay out of the figure.
  clone.querySelectorAll('.chart-tools, [data-mark="tip"], .term-tip, details:not([open]) > :not(summary)')
    .forEach(n => n.remove());
  frame.querySelector('.export-body').append(clone);
  document.body.append(frame);
  return frame;
}

/**
 * Attach the tool row to a card.
 *
 * @param {HTMLElement} card The card element; its id names the files and the link.
 * @param {{
 *   title?: string,
 *   subtitle?: () => string,
 *   csv?: () => string | Promise<object[]>,
 *   target?: HTMLElement,
 *   file?: string
 * }} opts
 *   `title` defaults to the card's first heading. `subtitle` describes the
 *   current selection. `csv` returns SQL (run against the page's database) or
 *   rows already in hand. `target` is what the image shows (default: the card,
 *   minus its prose). `file` is the download basename (default: the card id).
 */
export function chartTools(card, opts = {}) {
  const id = card.id || 'chart';
  const file = opts.file ?? id;
  const title = () => opts.title ?? card.querySelector('h2, h3')?.textContent.trim() ?? 'Chart';
  const subtitle = () => (opts.subtitle ? opts.subtitle() : '');
  const bar = document.createElement('div');
  bar.className = 'chart-tools';
  bar.setAttribute('role', 'group');
  bar.setAttribute('aria-label', 'Download or share this chart');
  const button = (label, hint, fn) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.dataset.tip = hint;
    b.addEventListener('click', async () => {
      const prev = b.textContent;
      b.disabled = true;
      try {
        await fn(b);
      } catch (err) {
        console.warn(`${label} failed:`, err);
        b.textContent = 'Failed';
        setTimeout(() => { b.textContent = prev; }, 1500);
      } finally {
        b.disabled = false;
      }
    });
    bar.append(b);
  };
  const image = async render => {
    const node = opts.target ?? card.querySelector('.figure') ?? card;
    const frame = exportFrame({ title: title(), subtitle: subtitle(), node, link: cardLink(id) });
    try {
      // The frame sits off-screen so the page does not flash; the clone that
      // html-to-image renders would inherit that offset and come out blank,
      // so its position is reset on the clone only.
      return await render(frame, {
        pixelRatio: PIXEL_RATIO,
        backgroundColor: '#ffffff',
        cacheBust: false,
        style: { position: 'static', left: '0', top: '0', margin: '0' }
      });
    } finally {
      frame.remove();
    }
  };
  button('PNG', 'Download this chart as a PNG image, with its title, selection and source', async () => {
    download(`${file}.png`, await image(toPng));
  });
  button('SVG', 'Download this chart as an SVG image, with its title, selection and source', async () => {
    download(`${file}.svg`, await image(toSvg));
  });
  if (opts.csv) {
    button('Data (CSV)', 'Download the rows behind this chart, with the current filters', async () => {
      const src = await opts.csv();
      const rows = typeof src === 'string' ? await coordinator().query(src, { type: 'json' }) : src;
      const blob = new Blob([toCsv(Array.from(rows))], { type: 'text/csv' });
      const href = URL.createObjectURL(blob);
      download(`${file}.csv`, href);
      setTimeout(() => URL.revokeObjectURL(href), 1000);
    });
  }
  button('Copy link', 'Copy a link that opens this page with the same selection, at this chart', async b => {
    const link = cardLink(id);
    try {
      await navigator.clipboard.writeText(link);
      b.textContent = 'Copied';
    } catch {
      window.prompt('Copy this link', link);
    }
    setTimeout(() => { b.textContent = 'Copy link'; }, 1500);
  });
  const head = card.querySelector('h2, h3');
  (head?.parentElement === card ? head : card.firstElementChild)?.after(bar);
  return bar;
}

/**
 * Scroll to the card a shared link names (`#…&view=<id>`), once the page has
 * rendered. Read before the page's own state writer rewrites the hash.
 */
export function rememberLinkedView() {
  const id = new URLSearchParams(location.hash.replace(/^#/, '')).get('view');
  return () => {
    if (!id) return;
    const el = document.getElementById(id);
    if (!el) return;
    el.closest('details')?.setAttribute('open', '');
    el.scrollIntoView({ block: 'start' });
  };
}
