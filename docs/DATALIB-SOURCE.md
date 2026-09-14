# Fireflies as a datalib source

Turns a local Fireflies dump into rows in your
[datalib](https://github.com/imbue-ai/datalib) unified index, so meetings
are searchable next to everything else you have mirrored.

Run [`fireflies_dump.py`](../fireflies_dump.py) first — this reads the
dump, never the API. See [CREDENTIALS.md](CREDENTIALS.md).

## Why there is no Rust provider here

datalib's built-in sources are Rust crates behind a Bazel build. You do
not need one to add a source. Two facts in datalib's own design make a
plain script sufficient:

- **Any executable can be a step** — the contract is argv, environment
  variables, and NDJSON on stdout
  ([`step_protocol.md`](https://github.com/imbue-ai/datalib/blob/main/docs/dev/step_protocol.md)).
- **`grid_index` discovers sources by scanning the filesystem** for
  `<data_root>/<name>/render_markdown/indexed_markdown.doltlite_db`.
  There is no provider registry to register with. Write that file in the
  right place and the built-in index picks it up.

So a source is not something you register. It is a store you write where
the indexer already looks.

## Prerequisites

- datalib installed, and **`datalib-doltlite` on your `PATH`** — it ships
  in datalib's release tarball. These steps shell out to it rather than
  linking a library.
- Python 3.11+. Standard library only; the `requests` dependency belongs
  to the dump tool, not to these steps.
- A dump on disk.

## Wire it up

Copy [`config.example.toml`](../config.example.toml) into your data
root's `config.toml` and set the two absolute paths.

```toml
[[groups]]
id = "fireflies"
name = "Fireflies.ai"

[[steps]]
group = "fireflies"
function = "ingest"
command = "python3 /ABS/PATH/fireflies-liberation/datalib_source/ingest.py"
[steps.params.export]
path = "~/fireflies-dump"

[[steps]]
group = "fireflies"
function = "render_markdown"
command = "python3 /ABS/PATH/fireflies-liberation/datalib_source/render_markdown.py"
inputs = ["fireflies/ingest"]
[steps.params]
render_sig = "bump-me-on-render-changes"
```

Then add `fireflies/render_markdown` to the `inputs` of the shared
`grid_index` and `qmd_index` steps.

Four things that are easy to get wrong:

- **The group carries no `type`.** `type` selects a *built-in* provider;
  there is no built-in fireflies. A `type` here makes `datalib-step` try
  to dispatch and fail.
- **Paths in `command` must be absolute.** A step's working directory is
  the data root, not this repo.
- **`function = "render_markdown"` is load-bearing.** The step id is
  composed `<group>/<function>`, and that composed id is the directory
  `grid_index` scans for. Rename the function and the source vanishes
  from the index with no error.
- **`render_sig`** — see the trap below.

Validate before running: `datalib-dag --check <data_root>/config.toml`.

## What it reads and writes

```
~/fireflies-dump/            fireflies/ingest/          fireflies/render_markdown/
  transcripts/*.json    ->     entities.doltlite_db  ->   <id>/all.md
  media/                       transcripts                indexed_markdown.doltlite_db
  inventory.json               transcripts_bookkeeping             |
                               media                     unified_index/grid_index
```

`ingest` captures the dump into a raw store; `render_markdown` derives
markdown and the index store from it. The dump is read-only to both.

Two row kinds reach the grid:

| kind | one per | text |
|---|---|---|
| `Meeting` | transcript | the summary overview, or the title |
| `Meeting Segment` | ~1000-char passage | consecutive turns, `Speaker: text` per line |

Segments rather than one row per utterance is deliberate. Per-utterance,
about a third of all rows are "Yeah." and "Right." — a large store whose
search hits are four-word fragments. A passage returns with its context
around it. Full per-utterance detail stays in the markdown and the raw
store; only the grid projection is coarsened.

## Speaker aliases

Fireflies sometimes emits several spellings for one person. Collapse
them at render time:

```sh
cp datalib_source/aliases.example.json datalib_source/aliases.json
```

Only the `confirmed` block is applied. `proposed` is what the data
suggests and a human has not ratified — guessing there is how two people
quietly become one.

`aliases.json` is gitignored. A real alias table is a list of real people
and who you meet with; keep yours out of version control.

Names are canonicalised **before** consecutive turns are merged, not
after. An unresolved name splits one person's consecutive sentences into
two turns, so fixing it late corrects the speaker column and leaves the
turn boundaries wrong.

## Verify it worked

```sh
datalib-doltlite -readonly <data_root>/unified_index/grid_index/db.doltlite_db \
  "SELECT provider, kind, count(*) FROM grid_rows WHERE provider='fireflies' GROUP BY 1,2;"
```

`provider` is a plain string on datalib's read path, not an enum, which
is what lets a third-party source appear in the index and the UI with no
special-casing.

## Traps

Each of these fails **quietly** — the step reports success and the data
is wrong. That is what makes them expensive.

**A renderer change does not trigger a re-render.** datalib fingerprints
a step from its config entry and its inputs. The bytes of the script the
`command` points at are *not* part of that. Edit the renderer and the
step is skipped while reporting `done`, and every derived artifact keeps
the old output. Change `render_sig` in the config to force one re-run.
Not documented upstream; it cost us three full syncs to find.

**`datalib-doltlite` follows SQLite, where a backslash inside a string
literal is an ordinary character.** Escaping it the MySQL way doubles
every one. Since `json.dumps` writes a newline as the two characters
`\n`, a doubled backslash makes the payload unparseable on read.
Doubling the single quote is the only escape needed.

**Markdown alone reaches nothing.** A render step that writes `.md` files
but no `indexed_markdown.doltlite_db` contributes zero searchable rows,
and every step still reports success.

**`qmd_path` and `md_path` must be byte-identical**, or the document
drops out of free-text search silently. Derive both from one variable.

**Absent is not malformed.** No dump yet means emit nothing and exit 0.
A `data` failure poisons the shared index fan-in, blocking `grid_index`
for every other source you have.

**Multi-word filter values need quoting** when you query the index:
`kind:Meeting Segment` parses as `kind:Meeting` plus free text and
returns almost nothing. Write `kind:"Meeting Segment"`.

## Tests

```sh
cd datalib_source && python3 -m unittest discover -s tests
```

They run against your own dump and skip without one — 16 of 18 skip on a
fresh clone, which is the expected result. No fixture ships, because a
fixture built from real meetings is someone's private conversation.
