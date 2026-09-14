# Contributing

Small project, few rules. The ones below exist because breaking them has
cost real time.

## Ground rules

**A dump is immutable.** Never write to a dump directory, and never add
transcript or media content to this repository. For a closed account
nothing can rebuild a dump, and a fixture built from real meetings is
someone's private conversation. Tests read your own dump and skip
without one.

**Never commit `aliases.json`.** It is gitignored. A real alias table is
a list of real people and who you meet with. `aliases.example.json` is
the one that ships.

**Fail loudly.** Every bug worth reporting here so far has been a *quiet*
success: the step said `done` and the data was wrong. Prefer an error the
user cannot miss over a fallback that succeeds with the wrong answer, and
when you add a fallback, log when it fires.

**Verify against the tree, not the prose.** Upstream datalib docs are
detailed and occasionally stale; so are ours. If a doc and the code
disagree, the code wins — then fix the doc in the same change.

## Running things

```sh
pip install -r requirements.txt                        # dump tool only
cd datalib_source && python3 -m unittest discover -s tests
```

The datalib steps are standard library only, on purpose: they run under
whatever `python3` the DAG finds, not a virtualenv you control. Keep it
that way. They need `datalib-doltlite` on `PATH`.

## Known issues

Open, reproduced, and not yet fixed.

| # | Issue | Impact |
|---|---|---|
| 1 | **Row `uuid`s are raw Fireflies ids** (`<id>`, `<id>:b<n>`) rather than datalib's UUIDv5 recipe from [`entity_ids.md`](https://github.com/imbue-ai/datalib/blob/main/docs/dev/entity_ids.md). | Works today; Fireflies ids are ULID-shaped so collision is unlikely. But the id is also the markdown anchor and half a `/chat/{...}` URL, and changing it later invalidates both plus any stored feedback. Fixing it is a one-time re-render with new primary keys. |
| 2 | **`render_sig` is manual.** The step warns when it is absent, but nothing bumps it for you. | Forget it after a render change and datalib skips the step while reporting success. |
| 3 | **Speaker identity is per-transcript.** Fireflies emits several spellings for one person; only human-confirmed aliases are collapsed. | Cross-meeting speaker analysis undercounts until the alias table is filled in by hand. |
| 4 | **No `edges`.** Meetings that reference each other, or the same attendees, produce no graph. | The `edges` table stays empty for this source. |
| 5 | **Media is mirrored as metadata only.** Audio and video are downloaded by the dump tool but never rendered. | Media is not searchable or reachable from the index. |
| 6 | **`verify` checks the dump against the inventory, not against the API.** | A transcript deleted upstream between inventory and dump reads as a gap. |
| 7 | **Only `datalib_source/` is tested.** `fireflies_dump.py` has no tests; it was written against a live account under time pressure. | Regressions in the dump tool would not be caught. |

## Open specs

Work we want, roughly in order.

### 1. Port to Rust and propose it upstream

The intended destination. datalib's built-in sources are Rust crates
under Bazel; a Python step is the right way to *prototype* a connector
and the wrong way to ship one to everybody.

A provider there is three crates — `datalib_etl_fireflies_config` for the
config schema, `datalib_etl_fireflies` for ingest, and
`datalib_etl_fireflies_render` for render — with `Fireflies` added to the
`Provider` enum and the ingest method declared `Local` (it reads a dump)
or `Origin` (if it grows an API mode). See
[`data_architecture_ingestion_practices.md`](https://github.com/imbue-ai/datalib/blob/main/docs/dev/data_architecture_ingestion_practices.md)
and [`entity_ids.md`](https://github.com/imbue-ai/datalib/blob/main/docs/dev/entity_ids.md).

Doing that would resolve issues 1 and 2 for free: the id recipe is a
library call there, and a code change invalidates the build rather than
being invisible to it.

**This has not been discussed with the datalib maintainers yet.** Nothing
here should be read as an accepted plan on their side.

### 2. Fix the ids first

Issue 1 is cheap now and expensive later — every day the current scheme
runs, more anchors and URLs depend on it. Worth doing before a Rust port
rather than porting the deviation.

### 3. An API mode

Today the source reads a dump. A live ingest, incremental by
`transcript_updated_at`, would keep an active account current instead of
requiring a re-dump. Credentials would go through datalib's own
`latchkey` rather than the Keychain path used here.

### 4. Tests for the dump tool

At minimum: the GraphQL query builder, pagination, resume, and rate-limit
backoff, against recorded responses with real content stripped.

### 5. Render media

Attachment bytes are already mirrored. Making them reachable from the
rendered markdown, the way datalib's own providers do, would put meeting
audio one click from a search hit.

## Provenance

Written at an [Imbue](https://imbue.com) punk-software hackathon in
September 2026, for a real problem: a Fireflies account was closing and
several years of meeting transcripts were about to go with it. The
"punk" framing is Imbue's — human-centred rather than corporate-centred
software, and personal data ownership as the point rather than a
feature.

Not affiliated with Fireflies.ai or Imbue.
