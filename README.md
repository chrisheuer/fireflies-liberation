# fireflies-liberation

Get your Fireflies.ai meeting transcripts onto your own disk, then make
them searchable alongside the rest of your data with
[imbue-ai/datalib](https://github.com/imbue-ai/datalib).

Two independent pieces. Either works alone.

| | |
|---|---|
| [`fireflies_dump.py`](fireflies_dump.py) | pulls a Fireflies account to a local dump: transcript JSON, summaries, sentences, signed media. Resumable. No datalib required. |
| [`datalib_source/`](datalib_source/) | reads that dump as a datalib source, so meetings land in the unified index. |

Written because an account was closing and the transcripts were going
with it. If that is your situation, run the dump first and read the rest
afterwards.

## Get your transcripts out, step by step

Needs Python 3.11+ and `requests` (`pip install -r requirements.txt`).

### Step 1 — get your API key

Open <https://app.fireflies.ai/integrations/custom/fireflies> and copy
the key. Every plan tier can call the API; they differ in rate. Details
and the caveats: **[docs/CREDENTIALS.md](docs/CREDENTIALS.md)**.

### Step 2 — store it so you are not asked twice

On macOS, put it in the Keychain. Copy the key first, then:

```sh
security add-generic-password -a "$USER" -s FIREFLIES_API_KEY -w "$(pbpaste)"
```

`$(pbpaste)` rather than the key itself is deliberate — your shell
records the command *before* expanding it, so your history keeps the
harmless `$(pbpaste)` text instead of a live credential.

Anywhere else, `export FIREFLIES_API_KEY="..."`.

**Or skip both.** Run any command with no key configured and, if you are
at a terminal, it asks. Input is hidden, and the value never reaches your
environment, your shell history, or the process list.

### Step 3 — check the key works

```sh
python3 fireflies_dump.py whoami
```

Your account details mean you are in. An auth error means the key is
wrong or your plan has no API access.

### Step 4 — see what is there before pulling it

```sh
python3 fireflies_dump.py inventory
```

Writes `inventory.json` and prints counts. **One transcript is one
request**, so this number sets how long the dump takes: ~20 minutes for
1,000 on Business, ~2 days on Pro, ~20 days on Free.

### Step 5 — trial run

```sh
python3 fireflies_dump.py dump --max 5
```

Five transcripts into `~/fireflies-dump`. Look at them before spending
your rate limit on the rest.

### Step 6 — pull everything

```sh
python3 fireflies_dump.py dump
```

Resumable: re-run it and it collects what is missing. On a low tier, run
it daily. `--rpm` caps the request rate, default 60. `--out` moves the
dump; `--since` / `--until` bound it by date.

### Step 7 — confirm nothing is missing

```sh
python3 fireflies_dump.py verify
```

Checks the dump against `inventory.json` and names any gaps. Do this
before you close the account, not after.

### The dump is the source of truth

Everything downstream is derived and rebuildable. The dump is not — if
the account is gone, nothing regenerates it. Back it up, and never edit
a file inside it. To change how the data looks, change the renderer and
re-run the sync.

## How it plugs into datalib

[datalib](https://github.com/imbue-ai/datalib) mirrors your data from the
services that hold it into stores you own, renders it to markdown, and
builds two indexes over everything: a SQL one behind its grid, and a
semantic one for search. Its value is the *unified* part — one query
across every source at once.

It ships providers for Claude, ChatGPT, Slack, Gmail, Notion, Signal and
a couple of dozen more. Fireflies is not among them, and adding one
normally means a Rust crate and a Bazel build.

That is not required. Two facts in datalib's design make a plain script
enough:

- **Any executable can be a step.** The contract is argv, a handful of
  environment variables, and NDJSON on stdout
  ([`step_protocol.md`](https://github.com/imbue-ai/datalib/blob/main/docs/dev/step_protocol.md)).
- **Sources are discovered on the filesystem, not registered.**
  `grid_index` scans for
  `<data_root>/<name>/render_markdown/indexed_markdown.doltlite_db` and
  indexes whatever it finds.

So `datalib_source/` is two Python scripts that write that store. datalib
picks the meetings up on the next sync and they appear in the grid and in
semantic search beside every other source.

```
~/fireflies-dump/        fireflies/ingest/         fireflies/render_markdown/
  transcripts/*.json  ->   entities.doltlite_db ->   <id>/all.md
  media/                                             indexed_markdown.doltlite_db
                                                              |
                                                  unified_index/grid_index
```

Setup, configuration and the failure modes:
**[docs/DATALIB-SOURCE.md](docs/DATALIB-SOURCE.md)**. Needs
`datalib-doltlite` on `PATH` — it ships in datalib's release.

Meetings arrive as one `Meeting` row per transcript plus `Meeting
Segment` rows grouping consecutive turns into ~1000-character passages,
so a search hit comes back as a readable passage rather than a "Yeah."

## Where this came from

Built at an [Imbue](https://imbue.com) punk-software hackathon in
September 2026, for an actual problem: a Fireflies account was closing
and several years of meeting transcripts were going with it. "Punk"
is Imbue's framing — software centred on people rather than companies,
where owning your own data is the point rather than a feature.

The dump tool came first and stands alone. The datalib source came
second, once the transcripts were safe.

Not affiliated with Fireflies.ai or Imbue.

## Status

Run end to end against a real account: **1,101 meetings, 23,935 segment
rows**, into a datalib mirror alongside Claude and Otter transcripts.
Python 3.11+, macOS and Linux.

Not affiliated with Fireflies.ai or Imbue.

## To review and address

Known deviations and open work, carried here so they are visible rather
than buried. Full list with the reasoning:
**[CONTRIBUTING.md](CONTRIBUTING.md)**.

- **Row `uuid`s deviate from datalib's id recipe.** This source uses raw
  Fireflies ids (`<id>`, `<id>:b<n>`) for `grid_rows.uuid`, where
  datalib's [`entity_ids.md`](https://github.com/imbue-ai/datalib/blob/main/docs/dev/entity_ids.md)
  specifies a UUIDv5 over a namespaced recipe. It works — Fireflies ids
  are ULID-shaped, so a collision with another source is unlikely — but
  it is a deviation, and that id is also the markdown anchor and half of
  a `/chat/{...}` URL. Changing it later invalidates both, plus any
  feedback already filed against those ids. **Worth fixing before the
  ids spread further**, not after.
- **`render_sig` is manual.** The step warns when it is missing, but you
  have to bump it yourself after a render change.
- **A Rust port** is the intended path for contributing this upstream to
  datalib proper. Not yet discussed with the maintainers.
- **No tests for `fireflies_dump.py`** — it was written against a live
  account under time pressure.

## License

MIT — see [LICENSE](LICENSE).
