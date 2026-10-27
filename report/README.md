# report/

The report is **authored prose with measured numbers substituted into it**: not
a generated document, and not a hand-maintained one either.

| Path | Tracked? | What it is |
|---|---|---|
| `sections/*.md` | **yes** | The report itself: ~2,200 lines of hand-written narrative. Each file declares an anchor and is composed in filename order. |
| `draft.md`, `draft.html` | no | Rendered output of `uv run benchmark report`. |
| `assets/*.div.html`, `assets/*.png` | no | Charts, re-rendered on every build (the PNGs need kaleido and a Chrome; without them only the interactive fragments are written). |

## Regenerating

```bash
uv run benchmark report
uv run benchmark verify-figures   # exit 1 if a marked table disagrees with the database
```

`report` reads `results/benchmark.duckdb` (which ships with the repo via Git LFS)
and writes `draft.md`, `draft.html` and `assets/`. Nothing under `sections/` is
touched.

## Why the rendered files are not tracked

They are reproducible from the committed database, so tracking them only adds
churn: a 5.9 MB HTML diff on every build, and a generation timestamp that
changes even when no number does. Tracking them would also fight the pre-commit
hooks: `trailing-whitespace` rewrites generator output, so a freshly rendered
draft would never match a committed one byte-for-byte.

## How the numbers stay tied to the data

Authored sections tie their numbers to the database in two ways:

- `{{fact:...}}`, a single measured value inline in a sentence. `benchmark report`
  resolves every placeholder from the database at build time and fails on one it
  cannot resolve.
- Marked tables (`<!-- verify: name -->`): published as written, and recomputed
  by `uv run benchmark verify-figures`, which exits 1 if a published table has
  drifted from the data.

That is the point of the arrangement: the argument is written by a person, and
every number in it is re-derived from the results database.

## Status page

`uv run benchmark render-status --template <file>` renders an HTML page whose
`{{fact:...}}` placeholders resolve against the same database, so a summary page
cannot disagree with the report it summarizes. The template is yours to write:
`--template` is required, the repository ships none, and the facts it can name are
the keys of `FACTS` in `src/benchmark/report/narrative.py` (an unknown or
unresolvable one fails the render). The page is written beside the template as
`benchmark-status-<date>.html` unless `--output` names a file. HTML files directly
under `report/` are git-ignored, so a template or page kept there stays local.
