"""Markdown-to-HTML rendering.

Beyond what the generated sections emit (headings h1-h3, images, tables), the
hand-authored narrative sections use h4, bullet lists, blockquotes and hard-wrapped
prose. These tests pin that `####` renders as a heading rather than literally, and
that wrapped lines join into one paragraph rather than one each.
"""

from __future__ import annotations

from benchmark.report.html_export import markdown_to_html


def _render(tmp_path, md: str) -> str:
    src = tmp_path / "doc.md"
    src.write_text(md, encoding="utf-8")
    return markdown_to_html(src, tmp_path / "doc.html").read_text(encoding="utf-8")


def test_h4_renders_as_a_heading_not_literal_hashes(tmp_path):
    out = _render(tmp_path, "# Doc\n\n#### Best-value tier: solo vs loaded\n")

    assert "<h4" in out
    assert "#### Best-value" not in out


def test_repeated_headings_get_unique_anchors(tmp_path):
    """The report intentionally repeats headings across suites."""
    out = _render(tmp_path, "# Doc\n\n#### Value tier\n\ntext\n\n#### Value tier\n\ntext\n")

    assert 'id="value-tier"' in out
    assert 'id="value-tier-1"' in out


def test_hard_wrapped_lines_join_into_one_paragraph(tmp_path):
    """Authored prose wraps at ~80 chars; each line is not its own paragraph."""
    out = _render(tmp_path, "# Doc\n\nThe first line of prose\nwraps onto a second\nand a third.\n")

    assert out.count("<p>The first line") == 1
    assert "wraps onto a second and a third." in out
    assert out.count("<p>") == 1


def test_blank_line_still_separates_paragraphs(tmp_path):
    out = _render(tmp_path, "# Doc\n\nFirst para.\n\nSecond para.\n")

    assert out.count("<p>") == 2


def test_bullet_lists_render_as_lists(tmp_path):
    out = _render(tmp_path, "# Doc\n\n- first item\n- second item\n")

    assert "<ul>" in out
    assert out.count("<li>") == 2
    assert "<p>- first item</p>" not in out


def test_wrapped_bullet_continues_its_own_item(tmp_path):
    out = _render(tmp_path, "# Doc\n\n- an item whose text\n  wraps onto another line\n- second\n")

    assert out.count("<li>") == 2
    assert "wraps onto another line" in out


def test_blockquote_renders_and_joins_its_lines(tmp_path):
    out = _render(tmp_path, "# Doc\n\n> a quoted claim\n> continuing here\n")

    assert "<blockquote>" in out
    assert "a quoted claim continuing here" in out


def test_a_heading_ends_the_preceding_paragraph(tmp_path):
    """Paragraph gathering must not swallow the next section's heading."""
    out = _render(tmp_path, "# Doc\n\nsome prose\n## Next Section\n\nmore prose\n")

    assert "<h2" in out
    assert "Next Section</p>" not in out


def test_table_still_renders_after_the_block_changes(tmp_path):
    out = _render(tmp_path, "# Doc\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n")

    assert "<table>" in out and "<td>1</td>" in out
