# fireflies-liberation

Get your Fireflies.ai meeting transcripts onto your own disk, then make
them searchable alongside the rest of your data with
[imbue-ai/datalib](https://github.com/imbue-ai/datalib).

Two independent pieces. Use either alone.

| | |
|---|---|
| [`fireflies_dump.py`](fireflies_dump.py) | pulls a Fireflies account to a local dump: full transcript JSON, summaries, sentences, and signed media. Resumable. No datalib required. |
| [`datalib_source/`](datalib_source/) | reads that dump as a datalib source, so meetings land in the unified index next to your other conversations. |

Written because an account was closing and the transcripts were going
with it. If that is your situation, run the dump first and read the rest
later.

## Get your data out

Needs Python 3.11+ and `requests`.

```sh
pip install -r requirements.txt

# Your key: fireflies.ai -> Settings -> Developer Settings
export FIREFLIES_API_KEY="..."          # or store it in the macOS Keychain:
# security add-generic-password -a "$USER" -s FIREFLIES_API_KEY -w "$(pbpaste)"

python3 fireflies_dump.py whoami                    # confirm the key works
python3 fireflies_dump.py inventory                 # list what exists, write inventory.json
python3 fireflies_dump.py dump --max 5              # trial run, 5 transcripts
python3 fireflies_dump.py dump                      # everything, resumable
python3 fireflies_dump.py verify                    # check the dump against the inventory
```

Output goes to `~/fireflies-dump` (`--out` to change it). `dump` is
resumable: re-run it and it picks up what is missing.

**Mind the rate limits.** Per Fireflies' docs: Free 50/day, Pro 500/day,
Business/Enterprise 60/min. One transcript is one request. `--rpm`
caps the rate, default 60.

**The key never lands anywhere.** It is read from the environment or the
Keychain and never written to a file, a log line, or the dump.

### The dump is the source of truth

Everything downstream is derived and can be rebuilt. The dump cannot —
if the account is gone, nothing can regenerate it. Back it up, and never
edit a file inside it. To change how the data looks, change the
renderer and re-run the sync.

## Use it with datalib

datalib has no built-in Fireflies provider, and it does not need one:
[any executable can be a step](https://github.com/imbue-ai/datalib/blob/main/docs/dev/step_protocol.md),
and `grid_index` finds a source by scanning for
`<data_root>/<name>/render_markdown/indexed_markdown.doltlite_db`. This
writes exactly that, in Python, with no dependency beyond the
`datalib-doltlite` binary that ships in datalib's own release.

```
~/fireflies-dump/          fireflies/ingest/           fireflies/render_markdown/
  transcripts/*.json   ->    entities.doltlite_db  ->    <id>/all.md
  media/                                                 indexed_markdown.doltlite_db
                                                                  |
                                                    unified_index/grid_index  (built-in)
```

Copy [`config.example.toml`](config.example.toml) into your data root's
`config.toml`, set the absolute paths, and sync.

Meetings arrive as two row kinds: one `Meeting` per transcript, and
`Meeting Segment` rows grouping consecutive turns into ~1000-character
passages — a readable passage rather than a "Yeah." fragment, and a
better unit for search to return.

### Speaker aliases

Fireflies sometimes emits several spellings for one person ("Jane S" and
"Jane Smith"). `datalib_source/identity.py` collapses them at render
time.

```sh
cp datalib_source/aliases.example.json datalib_source/aliases.json
```

Only the `confirmed` block is applied. `proposed` is what the data
suggests and a human has not ratified — guessing there is how two people
quietly become one. `aliases.json` is gitignored on purpose: a real
alias table is a list of real people and who you meet with.

## Tests

```sh
cd datalib_source && python3 -m unittest discover -s tests
```

They run against **your own dump**, not a checked-in fixture, and skip
when there is no dump (16 of 18 skip on a fresh clone -- that is the
expected result, not a failure). No transcript or media content is in
this repository, and none should ever be added: a fixture built from
real meetings is someone's private conversation.

## Things that cost us time

Written down because each one fails *quietly* — the step reports success
and the data is wrong.

- **A renderer change does not trigger a re-render.** datalib fingerprints
  a step from its config entry and its inputs, not the bytes of the
  script the `command` points at. Edit the renderer and the step is
  skipped while reporting `done`. Bump `render_sig` in the config to
  force it. This cost three full syncs to find.
- **`datalib-doltlite` follows SQLite, where a backslash in a string
  literal is literal.** Escaping it the MySQL way doubles every one, and
  since `json.dumps` writes a newline as `\n`, JSON payloads come back
  unparseable. Doubling the single quote is the only escape needed.
- **Markdown alone reaches nothing.** A render step that writes `.md`
  files but no `indexed_markdown.doltlite_db` contributes zero
  searchable rows, and every step still reports success.
- **`qmd_path` and `md_path` must be byte-identical**, or the document
  drops out of free-text search with no error.
- **Absent is not malformed.** No dump yet means emit nothing and exit 0.
  A `data` failure poisons the shared index fan-in for every other
  source.

## Status

Run against a real account: 1,101 meetings, ~24,000 segment rows, into a
datalib mirror alongside Claude and Otter transcripts. Python 3.11+,
macOS and Linux. `datalib_source/` needs the `datalib-doltlite` binary
from a datalib release on `PATH`.

Not affiliated with Fireflies.ai or Imbue.

## License

MIT — see [LICENSE](LICENSE).
