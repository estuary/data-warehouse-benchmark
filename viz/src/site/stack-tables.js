/**
 * Phone layout for the data tables: below 640 px each row becomes a card, and
 * each cell carries its column heading as a label (site.css draws the label
 * from `data-label`). A 950 px table in a 312 px column would show the engine
 * names and a sliver of the first number, with every other column, and so the
 * answer, off to the right.
 *
 * Tables are rendered and re-rendered by page code at any time, so labelling
 * runs from a MutationObserver rather than once. It only adds attributes; the
 * desktop layout is untouched. The ARIA roles keep the table semantics that
 * some screen readers drop once CSS changes a table's `display`.
 *
 * Opt out with `no-stack` on a table that must keep its columns (the query
 * matrix on the Query explorer, which scrolls with its first column pinned).
 */

const TABLES = 'table.data-table:not(.no-stack)';

/**
 * Column headings by column index, resolving `colspan` and `rowspan` across
 * every header row, so a two-row header ("At 100 GB" over "Times as fast")
 * labels its cells "At 100 GB · Times as fast".
 * @param {HTMLTableElement} table
 * @returns {string[]}
 */
function columnLabels(table) {
  const rows = [...(table.tHead?.rows ?? [])];
  const grid = [];
  rows.forEach((row, r) => {
    grid[r] ??= [];
    let c = 0;
    for (const cell of row.cells) {
      while (grid[r][c] !== undefined) c += 1;
      const text = cell.textContent.replace(/\s+/g, ' ').trim();
      for (let dr = 0; dr < (cell.rowSpan || 1); dr += 1) {
        grid[r + dr] ??= [];
        for (let dc = 0; dc < (cell.colSpan || 1); dc += 1) grid[r + dr][c + dc] = text;
      }
      c += cell.colSpan || 1;
    }
  });
  const width = Math.max(0, ...grid.map(r => r.length));
  return Array.from({ length: width }, (_, c) =>
    [...new Set(grid.map(r => r[c]).filter(Boolean))].join(' · '));
}

/**
 * Label one table's cells with their column headings.
 * @param {HTMLTableElement} table
 */
export function labelTable(table) {
  const labels = columnLabels(table);
  table.setAttribute('role', 'table');
  for (const group of [table.tHead, ...table.tBodies]) group?.setAttribute('role', 'rowgroup');
  for (const th of table.tHead?.querySelectorAll('th') ?? []) th.setAttribute('role', 'columnheader');
  for (const body of table.tBodies) {
    for (const row of body.rows) {
      row.setAttribute('role', 'row');
      let c = 0;
      for (const cell of row.cells) {
        cell.setAttribute('role', 'cell');
        const label = labels[c];
        if (label && cell.dataset.label !== label) cell.dataset.label = label;
        c += cell.colSpan || 1;
      }
    }
  }
}

/**
 * Label every data table under `root` now and whenever one is added or redrawn.
 * @param {HTMLElement} root
 */
export function stackTables(root = document.body) {
  const run = () => root.querySelectorAll(TABLES).forEach(labelTable);
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; run(); });
  }).observe(root, { childList: true, subtree: true });
  run();
}
