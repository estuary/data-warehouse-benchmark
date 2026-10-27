"""Inspect and scaffold benchmark settings.

Subcommands:
    settings show                   # compact: ENV VAR / DEFAULT / CURRENT / SOURCE
    settings show --descriptions    # add SECTION + DESCRIPTION columns
    settings show -s SNOWFLAKE      # restrict to one section
    settings generate               # combined preview to stdout
    settings generate --dir env     # one env/<section>.env.example per section
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Annotated

import typer
from pydantic import SecretStr
from pydantic_settings import BaseSettings
from rich.console import Console
from rich.table import Table
from rich.text import Text

# Diagnostic commands tolerate unknown env vars (stale entries from a previous
# schema, vars that haven't been migrated yet) so the operator can still run
# `settings show` / `generate` to figure out what needs to change. Must land
# before `benchmark.settings` is imported so its module-level _EXTRA lookup
# sees it.
os.environ.setdefault("BENCHMARK_SETTINGS_EXTRA", "ignore")

from benchmark.settings import BenchmarkSettings, get_settings

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_prefix(model: BaseSettings) -> str:
    return model.model_config.get("env_prefix", "") or ""


def _is_nested_settings(model: BaseSettings, field_name: str) -> bool:
    return isinstance(getattr(model, field_name), BaseSettings)


def _normalize_docstring(doc: str | None) -> str:
    if not doc:
        return ""
    return " ".join(line.strip() for line in doc.strip().splitlines() if line.strip())


def _format_value(value: object, show_secrets: bool) -> Text:
    if value is None:
        return Text("∅", style="dim")
    if isinstance(value, SecretStr):
        if show_secrets:
            return Text(value.get_secret_value(), style="yellow")
        return Text("●●● (set)", style="green")
    if isinstance(value, list):
        if not value:
            return Text("[]", style="dim")
        return Text(", ".join(map(str, value)))
    if isinstance(value, str) and not value:
        return Text('""', style="dim")
    return Text(str(value))


def _format_for_env(value: object) -> str:
    """Render a value as the right-hand side of a KEY=VALUE line in .env."""
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return ""  # never leak secrets into a template
    if isinstance(value, list):
        # Single-quote the JSON array: it survives a shell sourcing/exporting the
        # env file (e.g. mise) without bash mangling the brackets/double-quotes.
        return f"'{json.dumps(value)}'"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value
    return str(value)


_SECTION_ORDER = [
    ("ROOT", None),
    ("AWS", "AWS"),
    ("ESTUARY", "ESTUARY"),
    ("SNOWFLAKE", "SNOWFLAKE"),
    ("DATABRICKS", "DATABRICKS"),
    ("BIGQUERY", "BIGQUERY"),
    ("REDSHIFT", "REDSHIFT"),
    ("FABRIC", "FABRIC"),
    ("CLICKHOUSE", "CLICKHOUSE"),
    ("MOTHERDUCK", "MOTHERDUCK"),
]


def _iter_sections(settings: BenchmarkSettings):
    for title, attr in _SECTION_ORDER:
        model = settings if attr is None else getattr(settings, attr)
        yield title, _env_prefix(model), model


# ---------------------------------------------------------------------------
# `show`
# ---------------------------------------------------------------------------


@app.command()
def show(
    section: Annotated[
        str | None,
        typer.Option(
            "--section",
            "-s",
            help="Print only one section (e.g. SNOWFLAKE, BIGQUERY, ROOT). Case-insensitive.",
        ),
    ] = None,
    descriptions: Annotated[
        bool,
        typer.Option(
            "--descriptions",
            "-d",
            help="Add SECTION and DESCRIPTION columns with class docstrings + per-field descriptions.",
        ),
    ] = False,
    show_secrets: Annotated[
        bool,
        typer.Option(
            "--show-secrets",
            help="Reveal SecretStr values in plaintext. Off by default.",
        ),
    ] = False,
) -> None:
    """Render every settings field with its default, current value, and source."""
    get_settings.cache_clear()
    settings = get_settings()

    table = Table(
        show_header=True,
        header_style="bold cyan",
        expand=True,
        pad_edge=False,
    )
    if descriptions:
        table.add_column("SECTION", style="bold magenta", ratio=3, no_wrap=False)
    table.add_column("ENV VAR", style="bold", ratio=3, no_wrap=False)
    table.add_column("DEFAULT", style="dim", ratio=2, no_wrap=False)
    table.add_column("CURRENT", ratio=3, no_wrap=False)
    table.add_column("SOURCE", style="dim", ratio=1, no_wrap=False)
    if descriptions:
        table.add_column("DESCRIPTION", style="italic", ratio=5, no_wrap=False)

    def _row(*cells: object) -> list[object]:
        """Drop SECTION/DESCRIPTION cells when descriptions are off."""
        ordered = list(cells)
        if not descriptions:
            # cell order: SECTION, ENV VAR, DEFAULT, CURRENT, SOURCE, DESCRIPTION
            ordered = [ordered[1], ordered[2], ordered[3], ordered[4]]
        return ordered

    target = section.upper() if section else None
    rendered_any = False
    prev_title: str | None = None

    for title, prefix, model in _iter_sections(settings):
        if target and title != target:
            continue

        fields_set = model.model_fields_set
        rows: list[tuple[str, object, object, bool, str]] = []
        for field_name, field_info in model.__class__.model_fields.items():
            if _is_nested_settings(model, field_name):
                continue
            env_name = f"{prefix}{field_name}"
            default = field_info.get_default(call_default_factory=True)
            current = getattr(model, field_name)
            is_set = field_name in fields_set
            description = field_info.description or ""
            rows.append((env_name, default, current, is_set, description))

        if not rows:
            continue

        if prev_title is not None:
            table.add_section()

        section_label: Text | str = ""
        if descriptions:
            section_label = Text(title, style="bold magenta")
            section_doc = _normalize_docstring(model.__class__.__doc__)
            if section_doc:
                section_label.append("\n")
                section_label.append(section_doc, style="not bold dim")

        for i, (env_name, default, current, is_set, description) in enumerate(rows):
            source = "env" if is_set else "default"
            source_style = "green" if is_set else "dim"
            table.add_row(
                *_row(
                    section_label if i == 0 else "",
                    env_name,
                    _format_value(default, show_secrets=False),
                    _format_value(current, show_secrets=show_secrets),
                    Text(source, style=source_style),
                    description,
                )
            )

        prev_title = title
        rendered_any = True

    if not rendered_any:
        valid = ", ".join(t for t, _ in _SECTION_ORDER)
        console.print(f"[red]No section named {section!r}.[/red] Valid: {valid}")
        raise typer.Exit(code=1)

    console.print(table)


# ---------------------------------------------------------------------------
# `generate`
# ---------------------------------------------------------------------------


def _section_filename(attr: str | None) -> str:
    """Per-section env filename stem: ROOT -> 'benchmark', else the attr lower."""
    return "benchmark" if attr is None else attr.lower()


def _build_section_template(title: str, prefix: str, model: BaseSettings) -> str:
    """Template body for one section: header + KEY=default with descriptions."""
    lines: list[str] = []
    bar = "# " + "=" * 70
    lines.append(bar)
    lines.append(f"# {title} (prefix: {prefix or '—'})")
    section_doc = _normalize_docstring(model.__class__.__doc__)
    if section_doc:
        for wl in textwrap.wrap(section_doc, 68):
            lines.append(f"# {wl}")
    lines.append(bar)
    lines.append("")

    for field_name, field_info in model.__class__.model_fields.items():
        if _is_nested_settings(model, field_name):
            continue
        env_name = f"{prefix}{field_name}"
        default = field_info.get_default(call_default_factory=True)
        description = field_info.description or ""
        if description:
            for wl in textwrap.wrap(description, 75):
                lines.append(f"# {wl}")
        lines.append(f"{env_name}={_format_for_env(default)}")
        lines.append("")

    return "\n".join(lines)


def _build_env_template(settings: BenchmarkSettings) -> str:
    """Combined preview of every section (for stdout / --outfile)."""
    lines: list[str] = [
        "# Generated by `settings generate` — combined preview of every section.",
        "# For real use run `settings generate --dir env` to write one",
        "# env/<section>.env.example per section, then copy each to env/<section>.env.",
        "",
    ]
    for title, prefix, model in _iter_sections(settings):
        lines.append(_build_section_template(title, prefix, model))
    return "\n".join(lines)


@app.command()
def generate(
    directory: Annotated[
        Path | None,
        typer.Option(
            "--dir",
            "-d",
            help="Write one <section>.env.example per section into this directory "
            "(e.g. env). Copy each to <section>.env and fill it in.",
        ),
    ] = None,
    outfile: Annotated[
        Path | None,
        typer.Option(
            "--outfile",
            "-o",
            help="Write the combined preview here. If omitted (and no --dir), dumps to stdout.",
        ),
    ] = None,
) -> None:
    """Emit env templates with current defaults and per-field descriptions.

    With ``--dir`` writes one ``<section>.env.example`` per section (the harness
    reads ``env/<section>.env``, so copy each ``.example`` and fill it in).
    Without ``--dir`` emits a combined preview to stdout or ``--outfile``.
    """
    get_settings.cache_clear()
    settings = get_settings()

    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        written = 0
        for title, attr in _SECTION_ORDER:
            model = settings if attr is None else getattr(settings, attr)
            content = _build_section_template(title, _env_prefix(model), model)
            (directory / f"{_section_filename(attr)}.env.example").write_text(
                content + "\n", encoding="utf-8"
            )
            written += 1
        console.print(f"[green]Wrote[/green] {written} <section>.env.example files to {directory}/")
        return

    content = _build_env_template(settings)
    if outfile is None:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
        return

    outfile.write_text(content, encoding="utf-8")
    console.print(f"[green]Wrote[/green] {outfile} ([dim]{len(content)} bytes[/dim])")


if __name__ == "__main__":
    app()
