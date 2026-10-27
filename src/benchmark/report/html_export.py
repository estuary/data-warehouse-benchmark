"""Package the markdown report draft into a single self-contained HTML file.

``report/draft.md`` references chart PNGs under ``report/assets/``. For a
shareable artifact we want one file with no external dependencies, so this
module renders the markdown to HTML. For each referenced ``assets/x.png``, if
an interactive plotly fragment ``assets/x.div.html`` exists (written by the
report generator) it is embedded instead — and plotly.js is injected inline
ONCE into ``<head>`` — otherwise the image is inlined as a base64 ``data:``
URI. Either way the output stays a single file with no external requests.
The styling matches the canonical report look (a centered wide column,
bordered zebra-striped tables, ``<code>`` chips).

It deliberately supports only the markdown subset the report generator emits —
ATX headers (``#``/``##``/``###``), GFM pipe tables, paragraphs, image links
(``![alt](assets/x.png)``), and inline ``**bold**`` / ``_em_`` / `` `code` ``.
That keeps it dependency-free (no markdown library) and predictable, since the
input is machine-generated rather than arbitrary prose.
"""

from __future__ import annotations

import base64
import functools
import html
import logging
import re
from pathlib import Path

logger = logging.getLogger("benchmark.report.html_export")

CSS = """body { font-family: -apple-system, sans-serif; max-width: 1600px; margin: 2em auto; padding: 0 1em; line-height: 1.6; }
table { border-collapse: collapse; width: 100%; margin: 0.6em 0 0.2em; font-size: 0.85em; }
details.tbl { margin: 1em 0; }
details.tbl > summary { cursor: pointer; display: inline-block; padding: 3px 10px 3px 6px;
  font-size: 0.8em; letter-spacing: 0.04em; text-transform: uppercase; color: #555;
  background: #f2f2f2; border: 1px solid #ddd; border-radius: 3px; user-select: none; }
details.tbl > summary:hover { background: #e9e9e9; color: #222; }
details.tbl > summary:focus-visible { outline: 2px solid #2a5db0; outline-offset: 2px; }
details.tbl[open] > summary { margin-bottom: 0.2em; }
.tblctl { font-size: 0.85em; margin: 1em 0; }
.tblctl button { font: inherit; cursor: pointer; padding: 4px 12px; border-radius: 3px;
  border: 1px solid #ccc; background: #fff; color: #333; }
.tblctl button:hover { background: #f2f2f2; }
th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: left; }
th { background: #f5f5f5; }
tr:nth-child(even) { background: #fafafa; }
img { max-width: 100%; height: auto; margin: 1em 0; }
code { background: #f0f0f0; padding: 2px 5px; border-radius: 3px; font-size: 0.9em; }
h1 { border-bottom: 2px solid #333; padding-bottom: 0.3em; }
h2 { border-bottom: 1px solid #ddd; padding-bottom: 0.2em; margin-top: 2em; }
h1, h2, h3, h4 { scroll-margin-top: 1em; }
h4 { margin: 1.6em 0 0.4em; font-size: 1.02em; }
ul { margin: 0.7em 0; padding-left: 1.4em; }
li { margin: 0.3em 0; }
blockquote { margin: 1em 0; padding: 0.1em 0 0.1em 1em; border-left: 3px solid #bbb; color: #444; }
nav.toc { background: #fafafa; border: 1px solid #ddd; border-radius: 6px; padding: 0.8em 1.2em; margin: 1.5em 0; font-size: 0.9em; }
nav.toc ul { margin: 0.3em 0; padding-left: 1.2em; list-style: none; }
nav.toc > ul { padding-left: 0; }
nav.toc a { text-decoration: none; color: #2a5db0; }
nav.toc a:hover { text-decoration: underline; }
nav.toc .toc-h1 { font-weight: 600; margin-top: 0.4em; }
@media (prefers-color-scheme: dark) {
  body { background: #1a1a1a; color: #e0e0e0; }
  th { background: #2a2a2a; } tr:nth-child(even) { background: #222; }
  th, td { border-color: #444; }
  code { background: #2a2a2a; }
  nav.toc { background: #222; border-color: #444; }
  nav.toc a { color: #7aa2e3; }
  blockquote { border-left-color: #555; color: #b8b8b8; }
  details.tbl > summary { background: #262626; border-color: #444; color: #bbb; }
  details.tbl > summary:hover { background: #303030; color: #eee; }
  .tblctl button { background: #262626; border-color: #444; color: #ddd; }
  .tblctl button:hover { background: #303030; }
}
@media print {
  details.tbl > summary { display: none; }
  .tblctl { display: none; }
}"""

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
_TABLE_SEP_RE = re.compile(r"^\|[\s:|-]+\|?\s*$")

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
}


def _inline(text: str) -> str:
    """Escape HTML then apply inline markdown (code → bold → em, in that order)."""
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", text)
    return text


@functools.cache
def _plotly_js() -> str | None:
    """The full plotly.js source for inline injection (cached — it's ~4 MB),
    or ``None`` if plotly isn't importable (charts then fall back to their
    static PNGs)."""
    try:
        from plotly.offline import get_plotlyjs
    except ImportError:
        return None
    return get_plotlyjs()


def _embed_image(alt: str, rel_path: str, base_dir: Path) -> tuple[str, bool]:
    """Embed a chart reference. Prefers the interactive plotly fragment
    (``x.div.html`` next to ``x.png``) when it exists and plotly is importable;
    falls back to the PNG inlined as a base64 data URI. Returns
    ``(html, used_plotly_fragment)``."""
    src = (base_dir / rel_path).resolve()
    div = src.parent / (src.stem + ".div.html")
    if div.exists() and _plotly_js() is not None:
        return f'<div class="plotly-chart">{div.read_text(encoding="utf-8")}</div>', True
    if not src.exists():
        logger.warning("report image missing, leaving placeholder: %s", src)
        return f"<p><em>[missing image: {html.escape(rel_path)}]</em></p>", False
    mime = _MIME_BY_SUFFIX.get(src.suffix.lower(), "application/octet-stream")
    b64 = base64.b64encode(src.read_bytes()).decode("ascii")
    return f'<img src="data:{mime};base64,{b64}" alt="{html.escape(alt)}">', False


def _render_table(lines: list[str], start: int) -> tuple[list[str], int]:
    """Render a GFM pipe table starting at ``start`` (header + separator rows
    already validated by the caller). Returns (html_lines, next_index)."""

    def cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    header = cells(lines[start])
    i = start + 2  # skip header + separator
    out = [
        "<table>",
        "<thead>",
        "<tr>",
        *[f"<th>{_inline(h)}</th>" for h in header],
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    rows = 0
    while i < len(lines) and lines[i].startswith("|"):
        out += ["<tr>", *[f"<td>{_inline(c)}</td>" for c in cells(lines[i])], "</tr>"]
        rows += 1
        i += 1
    out += ["</tbody>", "</table>"]
    # Collapsed by default. The report carries dozens of wide tables and reading
    # it end to end means scrolling past all of them; the prose is the argument
    # and the tables are the evidence you open when you want to check it.
    # <details> is used deliberately over a JS toggle: it works with keyboard and
    # screen readers, survives with scripting disabled, and prints predictably.
    shape = f"{rows} row{'s' if rows != 1 else ''} &times; {len(header)} columns"
    return (
        ['<details class="tbl">', f"<summary>Table &middot; {shape}</summary>", *out, "</details>"],
        i,
    )


def markdown_to_html(
    md_path: Path,
    output_path: Path | None = None,
    title: str = "2026 Benchmark Report",
) -> Path:
    """Convert a report markdown file to a single self-contained HTML file.

    Args:
        md_path: the ``draft.md`` to convert. Image links are resolved relative
            to this file's directory and inlined as base64.
        output_path: where to write the HTML. Defaults to ``md_path`` with a
            ``.html`` suffix (e.g. ``report/draft.html``).
        title: the document ``<title>``.

    Returns:
        The path the HTML was written to.
    """
    if output_path is None:
        output_path = md_path.with_suffix(".html")
    base_dir = md_path.parent
    lines = md_path.read_text(encoding="utf-8").splitlines()

    body: list[str] = []
    toc: list[tuple[int, str, str]] = []  # (level, text, slug)
    seen_slugs: dict[str, int] = {}
    uses_plotly = False
    i = 0
    while i < len(lines):
        line = lines[i]
        header = _HEADER_RE.match(line)
        image = _IMAGE_RE.match(line)
        is_table = (
            line.startswith("|")
            and i + 1 < len(lines)
            and _TABLE_SEP_RE.match(lines[i + 1]) is not None
        )
        if header:
            level = len(header.group(1))
            text = header.group(2)
            # Slugify + dedup: the report intentionally repeats headings across
            # families (## Latency Analysis under both suites), so each needs a
            # unique anchor for the ToC to target the right one.
            base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
            n = seen_slugs.get(base, 0)
            seen_slugs[base] = n + 1
            slug = base if n == 0 else f"{base}-{n}"
            body.append(f'<h{level} id="{slug}">{_inline(text)}</h{level}>')
            if level <= 2:  # H1/H2 only — deeper nesting is too granular for a ToC
                toc.append((level, text, slug))
            i += 1
        elif image:
            embedded, used = _embed_image(image.group(1), image.group(2), base_dir)
            body.append(embedded)
            uses_plotly = uses_plotly or used
            i += 1
        elif is_table:
            table_lines, i = _render_table(lines, i)
            body += table_lines
        elif line.strip() == "":
            i += 1
        elif _QUOTE_RE.match(line):
            # Blockquote: consume the run, joining wrapped lines into one flow.
            quoted: list[str] = []
            while i < len(lines) and _QUOTE_RE.match(lines[i]):
                quoted.append(_QUOTE_RE.match(lines[i]).group(1).strip())
                i += 1
            body.append(f"<blockquote><p>{_inline(' '.join(quoted).strip())}</p></blockquote>")
        elif _LIST_RE.match(line):
            # Bullet list. A wrapped bullet continues on an indented line, so
            # non-marker lines attach to the current item rather than starting one.
            items: list[str] = []
            while i < len(lines):
                m = _LIST_RE.match(lines[i])
                if m:
                    items.append(m.group(1).strip())
                elif lines[i].strip() and items and not _HEADER_RE.match(lines[i]):
                    items[-1] += " " + lines[i].strip()
                else:
                    break
                i += 1
            rendered = "".join(f"<li>{_inline(t)}</li>" for t in items)
            body.append(f"<ul>{rendered}</ul>")
        else:
            # Paragraph: markdown joins consecutive lines into one flow, so a
            # hard-wrapped sentence in the authored sections stays one paragraph
            # rather than becoming a <p> per source line.
            para: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                if (
                    not nxt.strip()
                    or _HEADER_RE.match(nxt)
                    or _IMAGE_RE.match(nxt)
                    or _LIST_RE.match(nxt)
                    or _QUOTE_RE.match(nxt)
                    or nxt.startswith("|")
                ):
                    break
                para.append(nxt.strip())
                i += 1
            body.append(f"<p>{_inline(' '.join(para))}</p>")

    # Build a table of contents (H1/H2) and inject it right after the document's
    # first heading, so a 500-line single-page report is navigable and the
    # intentionally-repeated family headings resolve to the right anchor.
    if len(toc) > 2:
        nav = ['<nav class="toc"><strong>Contents</strong><ul>']
        for level, text, slug in toc[1:]:  # skip the doc title (first H1)
            cls = "toc-h1" if level == 1 else "toc-h2"
            nav.append(f'<li class="{cls}"><a href="#{slug}">{_inline(text)}</a></li>')
        nav.append("</ul></nav>")
        # Insert after the first rendered heading (the report title).
        first_h = next((k for k, ln in enumerate(body) if ln.startswith("<h1")), -1)
        body[first_h + 1 : first_h + 1] = ["\n".join(nav)]

    # Bulk control for the collapsed tables, and a print handler — CSS alone cannot
    # open a closed <details>, so printing without this loses every table. Both are
    # progressive enhancement: with scripting off the tables still open individually.
    table_count = sum(1 for ln in body if ln.startswith('<details class="tbl"'))
    if table_count:
        control = (
            '<div class="tblctl">'
            f'<button type="button" id="tbl-toggle" aria-expanded="false">'
            f"Expand all {table_count} tables</button></div>"
            "<script>(function(){"
            "var b=document.getElementById('tbl-toggle');"
            "var all=function(){return document.querySelectorAll('details.tbl')};"
            "b.addEventListener('click',function(){"
            "var open=b.getAttribute('aria-expanded')!=='true';"
            "all().forEach(function(d){d.open=open});"
            "b.setAttribute('aria-expanded',String(open));"
            f"b.textContent=(open?'Collapse all ':'Expand all ')+{table_count}+' tables';"
            "});"
            "window.addEventListener('beforeprint',function(){"
            "all().forEach(function(d){d.open=true})});"
            "})();</script>"
        )

    # plotly.js goes inline ONCE when any interactive fragment was embedded,
    # keeping the artifact a single self-contained file (no CDN request).
    if table_count:
        first_h = next((k for k, ln in enumerate(body) if ln.startswith("<h1")), -1)
        insert_at = (
            first_h + 2 if any(ln.startswith('<nav class="toc"') for ln in body) else first_h + 1
        )
        body[insert_at:insert_at] = [control]

    plotly_script = f"<script>{_plotly_js()}</script>\n" if uses_plotly else ""
    doc = (
        "<!DOCTYPE html>\n<html><head>"
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
        f"<title>{html.escape(title)}</title>\n"
        f"{plotly_script}"
        f"<style>\n{CSS}\n</style></head><body>\n" + "\n".join(body) + "\n</body></html>\n"
    )
    output_path.write_text(doc, encoding="utf-8")
    logger.info("wrote self-contained HTML report to %s", output_path)
    return output_path
