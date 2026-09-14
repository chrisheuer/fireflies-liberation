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

## Get your data out

Python 3.11+ and `requests`.

```sh
pip install -r requirements.txt

export FIREFLIES_API_KEY="..."        # or use the macOS Keychain -- see below

python3 fireflies_dump.py whoami      # does the key work?
python3 fireflies_dump.py inventory   # what exists -> inventory.json
python3 fireflies_dump.py dump --max 5   # trial run
python3 fireflies_dump.py dump           # everything, resumable
python3 fireflies_dump.py verify         # dump vs inventory
```

Output goes to `~/fireflies-dump` (`--out` to change). Re-running `dump`
picks up what is missing.

**Where the key lives, which plan you need, and how to store it safely:
[docs/CREDENTIALS.md](docs/CREDENTIALS.md).** Short version — the key is
at <https://app.fireflies.ai/integrations/custom/fireflies>, and the
Keychain is the recommended home:

```sh
security add-generic-password -a "$USER" -s FIREFLIES_API_KEY -w "$(pbpaste)"
```

`$(pbpaste)` keeps the key out of your shell history.

**One transcript is one request**, so the rate limit sets how long a dump
takes: 1,000 transcripts is ~20 minutes on Business, ~2 days on Pro, and
~20 days on Free. `dump` is resumable, so the slow tiers still work — run
it daily until `verify` is clean.

### The dump is the source of truth

Everything downstream is derived and rebuildable. The dump is not — if
the account is gone, nothing regenerates it. Back it up, and never edit
a file inside it. To change how the data looks, change the renderer and
re-run the sync.

## Put it in datalib

**[docs/DATALIB-SOURCE.md](docs/DATALIB-SOURCE.md)** — setup, config, what
it produces, and the traps.

Short version: datalib needs no Rust provider for this. Any executable
can be a step, and `grid_index` finds a source by scanning for
`<data_root>/<name>/render_markdown/indexed_markdown.doltlite_db`. These
two Python steps write exactly that. Copy
[`config.example.toml`](config.example.toml) into your data root, set the
absolute paths, sync.

Meetings arrive as one `Meeting` row per transcript plus `Meeting
Segment` rows grouping consecutive turns into ~1000-character passages —
a readable passage rather than a "Yeah." fragment.

Needs `datalib-doltlite` on `PATH` (it ships in datalib's release).

## Status

Run end to end against a real account: **1,101 meetings, 23,935 segment
rows**, into a datalib mirror alongside Claude and Otter transcripts.
Python 3.11+, macOS and Linux.

Not affiliated with Fireflies.ai or Imbue.

## License

MIT — see [LICENSE](LICENSE).
