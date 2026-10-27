/**
 * Click-to-sort for the data tables. A click on a column heading (or Enter or
 * Space on a focused one) cycles that column ascending, descending, and back to
 * the page's own order, which stays the default. Nothing about a table's
 * formatting changes: rows are only reordered, and the heading gains an arrow
 * (site.css draws it from `aria-sort`).
 *
 * Values are read from the rendered text, so page code needs no changes: a
 * leading number with its unit ($, ×, %, ms, s, min, GB, TB) sorts as a number,
 * anything else as text in natural order ("F4" before "F32"), and blanks such
 * as "Not run" always go last. A cell can override with `data-sort-value`.
 *
 * Tables are redrawn by page code at any time (a slider move re-renders the
 * people-served table), so, like stack-tables.js, this runs from a
 * MutationObserver, and a table redrawn in the same place keeps the sort the
 * reader chose.
 *
 * Left alone: tables that sort themselves in SQL (the Compare size table and
 * the Query explorer matrix, which carry `.sortable` headings or are not
 * `.data-table`), tables marked `no-sort`, and tables whose header or body
 * spans rows or columns, where reordering rows would break the grouping.
 */

const TABLES = 'table.data-table:not(.no-sort)';

/** Unit multipliers, so "1,000 ms" and "14.2 s", or "10 GB" and "1 TB", compare. */
const UNITS = { ms: 1, s: 1e3, min: 6e4, h: 3.6e6, gb: 1, tb: 1e3 };
const NUMBER = /^([-+]?)\$?(\d[\d,]*(?:\.\d+)?)\s*(ms|s|min|h|gb|tb|×|x|%)?(?=$|[\s(,;])/i;

/** Cells that hold no value: they sort last in either direction. */
const BLANK = /^(|—|-|n\/a|not run|not measured|no cost recorded)$/i;

/** The sort each redrawn table should keep, by where it sits on the page. */
const chosen = new Map();

/**
 * A cell's sort key: `{ n }` for a number, `{ t }` for text, null for a blank.
 * @param {HTMLTableCellElement} cell
 */
function keyOf(cell) {
  const raw = (cell?.dataset.sortValue ?? cell?.textContent ?? '').replace(/\s+/g, ' ').trim();
  if (BLANK.test(raw)) return null;
  const m = raw.match(NUMBER);
  if (m) {
    const unit = (m[3] ?? '').toLowerCase();
    return { n: Number(`${m[1]}${m[2].replace(/,/g, '')}`) * (UNITS[unit] ?? 1) };
  }
  return { t: raw.toLowerCase() };
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

/** Numbers before text, blanks last whichever the direction. */
function compare(a, b, desc) {
  if (a == null || b == null) return a == null ? (b == null ? 0 : 1) : -1;
  if ('n' in a !== 'n' in b) return 'n' in a ? -1 : 1;
  const d = 'n' in a ? a.n - b.n : collator.compare(a.t, b.t);
  return desc ? -d : d;
}

const sortable = table => {
  const head = table.tHead;
  if (!head || head.rows.length !== 1 || head.querySelector('.sortable, [data-sort]')) return false;
  if (table.querySelector('th[colspan], th[rowspan], td[colspan], td[rowspan]')) return false;
  return table.tBodies.length > 0 && [...table.tBodies].some(b => b.rows.length > 1);
};

/**
 * Put the table's rows in the order the column and direction ask for, or back
 * in the page's order when `dir` is none.
 */
function apply(table, col, dir) {
  const ths = [...table.tHead.rows[0].cells];
  ths.forEach((th, i) => th.setAttribute('aria-sort', i === col && dir !== 'none' ? dir : 'none'));
  for (const body of table.tBodies) {
    const rows = [...body.rows];
    rows.sort((a, b) => (dir === 'none' || col == null
      ? Number(a.dataset.sortIndex) - Number(b.dataset.sortIndex)
      : compare(keyOf(a.cells[col]), keyOf(b.cells[col]), dir === 'descending')
        || Number(a.dataset.sortIndex) - Number(b.dataset.sortIndex)));
    body.append(...rows);
  }
}

const NEXT = { none: 'ascending', ascending: 'descending', descending: 'none' };

/**
 * Make one table's headings sort it.
 * @param {HTMLTableElement} table
 */
export function enhanceTable(table) {
  if (table.dataset.sortReady || !sortable(table)) return;
  table.dataset.sortReady = '1';
  for (const body of table.tBodies) [...body.rows].forEach((r, i) => { r.dataset.sortIndex = String(i); });
  // Keyed by the nearest id and the table's place under it, since one card can hold several.
  const box = table.closest('[id]');
  const where = box ? `${box.id}:${[...box.querySelectorAll(TABLES)].indexOf(table)}` : null;
  const ths = [...table.tHead.rows[0].cells];
  ths.forEach((th, i) => {
    th.classList.add('can-sort');
    th.tabIndex = 0;
    th.setAttribute('aria-sort', 'none');
    const press = () => {
      const dir = NEXT[th.getAttribute('aria-sort')] ?? 'ascending';
      apply(table, i, dir);
      if (where) {
        if (dir === 'none') chosen.delete(where);
        else chosen.set(where, { label: th.textContent.trim(), dir });
      }
    };
    th.addEventListener('click', e => {
      // A glossary term or link inside the heading keeps its own click.
      if (e.target.closest('[data-term], a, button')) return;
      press();
    });
    th.addEventListener('keydown', e => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      press();
    });
  });
  // A redraw in the same place keeps the reader's sort, if that column is still there.
  const keep = where && chosen.get(where);
  if (keep) {
    const col = ths.findIndex(th => th.textContent.trim() === keep.label);
    if (col >= 0) apply(table, col, keep.dir);
  }
}

/**
 * Enhance every data table under `root` now and whenever one is added or redrawn.
 * @param {HTMLElement} root
 */
export function sortTables(root = document.body) {
  const run = () => root.querySelectorAll(TABLES).forEach(enhanceTable);
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; run(); });
  }).observe(root, { childList: true, subtree: true });
  run();
}
