#!/usr/bin/env python3
"""fireflies/render_markdown -- raw store -> markdown + the index store.

Writes two things under `<data_root>/fireflies/render_markdown/`:

  {transcript_id}/all.md      human-readable, one file per meeting
  indexed_markdown.doltlite_db  what `grid_index` actually reads

The second is not optional. `build_grid_index` skips any source without
it -- silently, with every step reporting success -- so markdown alone
reaches nothing.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (Doltlite, require_doltlite, StepEnv, log, outcome, progress_inc,  # noqa: E402
                     progress_length, progress_message, sql_str)
from identity import ALIASES_PATH, canonical, stats as identity_stats  # noqa: E402


def _render_sig() -> str:
    """Everything about THIS renderer that can change its output.

    The skip below compares fingerprints before reading a payload, and the
    payload-only fingerprint it used could not see a renderer change at
    all: edit the renderer or the alias table and every already-rendered
    meeting is skipped forever, while the step reports success. That is
    the quiet-success failure mode -- it looks identical to a fast run.

    Mixing this in means a renderer or alias change re-renders everything
    exactly once, then goes back to being incremental.
    """
    try:
        aliases = ALIASES_PATH.read_bytes()
    except OSError:
        aliases = b""
    return hashlib.sha256(RENDERER_VERSION.encode() + b"\x1f" + aliases).hexdigest()

PROVIDER = "fireflies"
SOURCE_LABEL = "Fireflies"
KIND_MEETING = "Meeting"
KIND_BLOCK = "Meeting Segment"
BLOCK_CHARS = 1000   # ~30-60s of speech: a readable passage, not a fragment
RENDERER_VERSION = "2"   # v2: speaker turns grouped into ~1000-char blocks
RENDER_SIG = _render_sig()   # must follow RENDERER_VERSION -- see _render_sig
STORE = "indexed_markdown.doltlite_db"
BATCH = 400            # grid rows per doltlite invocation

DDL = [
    """CREATE TABLE IF NOT EXISTS grid_rows (
        uuid VARCHAR(96) NOT NULL, provider VARCHAR(32) NOT NULL,
        kind VARCHAR(32) NOT NULL, source_label VARCHAR(32) NOT NULL,
        when_ts VARCHAR(40), when_ts_utc VARCHAR(40), when_offset VARCHAR(8),
        author VARCHAR(255), account VARCHAR(96), project VARCHAR(96),
        org_uuid VARCHAR(96), org_name VARCHAR(255), channel VARCHAR(255),
        conversation_name TEXT, conversation_uuid VARCHAR(96) NOT NULL,
        message_index INT, entire_chat VARCHAR(255) NOT NULL,
        text LONGTEXT NOT NULL, slack_link VARCHAR(512), qmd_path VARCHAR(512),
        source_url VARCHAR(1024), git_sha VARCHAR(64), upstream_id VARCHAR(128),
        upstream_entity_kind VARCHAR(32), upstream_scope VARCHAR(96),
        notion_page_uuid VARCHAR(96), notion_block_uuid VARCHAR(96),
        markdown_uuid VARCHAR(96), byte_size BIGINT, item_count BIGINT,
        PRIMARY KEY (uuid))""",
    """CREATE TABLE IF NOT EXISTS markdowns (
        markdown_uuid VARCHAR(96) NOT NULL, source_name VARCHAR(64) NOT NULL,
        provider VARCHAR(32) NOT NULL, kind VARCHAR(32) NOT NULL, title TEXT,
        created_at VARCHAR(40), updated_at VARCHAR(40), md_path VARCHAR(1024),
        source_fingerprint VARCHAR(64), upstream_cursor VARCHAR(64),
        row_set_hash CHAR(64), renderer_version VARCHAR(32),
        rendered_at VARCHAR(40), PRIMARY KEY (markdown_uuid))""",
    """CREATE TABLE IF NOT EXISTS edges (
        edge_uuid VARCHAR(96) NOT NULL, src_markdown_uuid VARCHAR(96),
        src_anchor_uuid VARCHAR(96), dst_markdown_uuid VARCHAR(96),
        dst_anchor_uuid VARCHAR(96), label VARCHAR(64),
        PRIMARY KEY (edge_uuid))""",
    """CREATE TABLE IF NOT EXISTS render_problems (
        uuid VARCHAR(96) NOT NULL, scope_key VARCHAR(255), scope_kind VARCHAR(32),
        source_name VARCHAR(64), stage VARCHAR(32), outcome VARCHAR(32),
        problems LONGTEXT, first_seen_at VARCHAR(40), last_seen_at VARCHAR(40),
        render_version VARCHAR(32), PRIMARY KEY (uuid))""",
    """CREATE TABLE IF NOT EXISTS source_measurements (
        subject VARCHAR(255) NOT NULL, kind VARCHAR(32) NOT NULL,
        measured_at VARCHAR(40), bytes BIGINT, items BIGINT,
        PRIMARY KEY (subject, kind))""",
]


def iso_utc(ms: int) -> str:
    return datetime.fromtimestamp((ms or 0)/1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def hhmmss(sec) -> str:
    h, rem = divmod(int(sec or 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def merge_turns(sentences: list[dict]) -> list[dict]:
    """Consecutive sentences by one speaker become one turn, so a row is a
    paragraph of speech rather than a fragment."""
    turns: list[dict] = []
    for s in sentences:
        # Canonicalise before the run-merge below, not after: Fireflies
        # emitted two spellings for one person ("Jane S" / "Jane Smith"),
        # and unresolved names break the merge as well as the author
        # column -- consecutive sentences by the same human would land as
        # two turns. Only human-confirmed aliases are applied; see
        # sources/identity/aliases.json.
        name = canonical(s.get("speaker_name")) or "Unknown"
        if turns and turns[-1]["speaker"] == name:
            turns[-1]["texts"].append(s.get("text") or "")
        else:
            turns.append({"speaker": name, "texts": [s.get("text") or ""],
                          "start": s.get("start_time") or 0})
    for t in turns:
        t["text"] = " ".join(x for x in t["texts"] if x).strip()
    return [t for t in turns if t["text"]]


def merge_blocks(turns: list[dict], max_chars: int = BLOCK_CHARS) -> list[dict]:
    """Group consecutive turns into ~max_chars passages.

    One row per speaker turn means 34% of rows are "Yeah." / "Right." --
    140k rows carrying 750KB of text, at ~19KB of store each. A block is
    also the better search unit: a hit arrives with the context around it
    instead of a four-word fragment. Full per-utterance detail stays in
    the markdown and the raw store; this only coarsens the projection.
    """
    blocks: list[dict] = []
    cur: dict | None = None
    for t in turns:
        if cur is None:
            cur = {"start": t["start"], "end": t["start"],
                   "speakers": [], "parts": [], "chars": 0}
        cur["parts"].append((t["speaker"], t["text"]))
        if t["speaker"] not in cur["speakers"]:
            cur["speakers"].append(t["speaker"])
        cur["chars"] += len(t["text"])
        cur["end"] = t["start"]
        if cur["chars"] >= max_chars:
            blocks.append(cur)
            cur = None
    if cur:
        blocks.append(cur)
    for b in blocks:
        # Turns already merge same-speaker runs, so every part changes speaker.
        b["text"] = "\n".join(f"{sp}: {tx}" for sp, tx in b["parts"])
    return blocks


def render_markdown(t: dict, media: list[dict]) -> str:
    """Front matter, then summary sections that exist, then speaker turns."""
    sents = t.get("sentences") or []
    speakers = t.get("speakers") or []
    s = t.get("summary") or {}
    turns = merge_turns(sents)
    # The signal is SENTENCES, not the speakers roster: 182 meetings carry a
    # full transcript with an empty roster (uploaded audio, where Fireflies
    # labels people "Speaker 1" on each sentence instead of naming them).
    no_speech = not sents
    speakers_identified = bool(speakers)
    speaker_names = ([x.get("name") for x in speakers] if speakers
                     else sorted({t["speaker"] for t in turns}))

    def yaml_list(items) -> str:
        clean = [str(x).replace('"', "'") for x in items if x]
        return "[" + ", ".join(f'"{x}"' for x in clean) + "]" if clean else "[]"

    att = [a.get("displayName") or a.get("name") or a.get("email")
           for a in (t.get("meeting_attendees") or [])]
    fm = [
        "---",
        f'id: "{t.get("id")}"',
        f'title: "{(t.get("title") or "").replace(chr(34), chr(39))}"',
        f'date: "{iso_utc(t.get("date") or 0)}"',
        f"duration_min: {round(float(t.get('duration') or 0), 2)}",
        f'organizer: "{t.get("organizer_email") or ""}"',
        f"participants: {yaml_list(t.get('participants') or [])}",
        f"attendees: {yaml_list(att)}",
        f"speakers: {yaml_list(speaker_names)}",
        "source: fireflies",
        f"no_speech: {'true' if no_speech else 'false'}",
        f"speakers_identified: {'true' if speakers_identified else 'false'}",
    ]
    if t.get("transcript_url"):
        fm.append(f'fireflies_url: "{t["transcript_url"]}"')
    if media:
        fm.append("media:")
        for m in media:
            field = (m.get("field") or "").replace("_url", "")
            fm.append(f'  {field}: "{m.get("rel_path")}"')
    fm.append("---")

    body = [f"\n# {t.get('title') or '(untitled meeting)'}\n"]

    if no_speech:
        body.append(
            "> No speech was recorded in this meeting. Fireflies captured the "
            "session but no audio was transcribed — typically a meeting where "
            "nobody else joined.\n")

    # Only emit sections that exist: 11 substantive meetings have no overview.
    for heading, key in (("Overview", "overview"), ("Short summary", "short_summary"),
                         ("Action items", "action_items"), ("Outline", "outline")):
        if s.get(key):
            body.append(f"## {heading}\n\n{s[key]}\n")
    if s.get("keywords"):
        body.append("## Keywords\n\n" + ", ".join(s["keywords"]) + "\n")
    if s.get("topics_discussed"):
        body.append("## Topics\n\n" + "\n".join(f"- {x}" for x in s["topics_discussed"]) + "\n")
    for sec in (s.get("extended_sections") or []):
        if sec.get("title") or sec.get("content"):
            body.append(f"## {sec.get('title') or 'Section'}\n\n{sec.get('content') or ''}\n")

    if turns:
        body.append("## Transcript\n")
        for t_ in turns:
            body.append(f"**[{hhmmss(t_['start'])}] {t_['speaker']}**\n\n{t_['text']}\n")

    return "\n".join(fm) + "\n".join(body)


def grid_rows_for(t: dict, qmd_path: str, md_uuid: str, md_bytes: int) -> list[dict]:
    tid = t.get("id")
    title = t.get("title") or ""
    when = iso_utc(t.get("date") or 0)
    account = t.get("organizer_email") or t.get("host_email")
    s = t.get("summary") or {}
    turns = merge_turns(t.get("sentences") or [])
    blocks = merge_blocks(turns)
    base = dict(provider=PROVIDER, source_label=SOURCE_LABEL, account=account,
                channel=title, conversation_name=title, conversation_uuid=tid,
                entire_chat=f"/chat/{tid}", qmd_path=qmd_path, markdown_uuid=md_uuid)

    rows = [dict(base, uuid=tid, kind=KIND_MEETING, when_ts=when, author="",
                 message_index=None,
                 text=s.get("overview") or s.get("gist") or title,
                 source_url=t.get("transcript_url"), upstream_id=tid,
                 upstream_entity_kind="transcript", byte_size=md_bytes,
                 item_count=len(blocks))]

    start = datetime.fromtimestamp((t.get("date") or 0)/1000, timezone.utc)
    for i, b in enumerate(blocks):
        ts = (start + timedelta(seconds=float(b["start"] or 0))
              ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        who = ", ".join(b["speakers"])[:255]     # author is VARCHAR(255)
        rows.append(dict(base, uuid=f"{tid}:b{i}", kind=KIND_BLOCK, when_ts=ts,
                         author=who, message_index=i, text=b["text"],
                         source_url=None, upstream_id=f"{tid}:b{i}",
                         upstream_entity_kind="segment", byte_size=None,
                         item_count=len(b["parts"])))
    return rows


COLS = ["uuid", "provider", "kind", "source_label", "when_ts", "author", "account",
        "project", "org_uuid", "org_name", "channel", "conversation_name",
        "conversation_uuid", "message_index", "entire_chat", "text", "slack_link",
        "qmd_path", "source_url", "git_sha", "upstream_id", "upstream_entity_kind",
        "upstream_scope", "notion_page_uuid", "notion_block_uuid", "markdown_uuid",
        "byte_size", "item_count"]


def row_sql(r: dict) -> str:
    vals = []
    for c in COLS:
        v = r.get(c)
        if c in ("message_index", "byte_size", "item_count"):
            vals.append("NULL" if v is None else str(int(v)))
        else:
            vals.append(sql_str(v) if v is not None else "NULL")
    return f"INSERT INTO grid_rows ({', '.join(COLS)}) VALUES ({', '.join(vals)})"


def main() -> int:
    env = StepEnv()
    try:
        require_doltlite()
    except RuntimeError as e:
        # A missing binary is the environment being wrong, not the
        # data. Say so plainly instead of dying inside subprocess.
        log(str(e), "error")
        outcome(env.step, failure="data")
        return 1
    out_dir = env.out_dir
    src = env.data_root / (env.inputs[0] if env.inputs else "fireflies/ingest")
    raw_db = src / "entities.doltlite_db"

    # Never downloaded is not a failure -- an empty render contributes
    # nothing rather than poisoning the shared index for other sources.
    if not raw_db.is_file():
        log(f"no raw store at {raw_db} -- nothing to render", "info")
        out_dir.mkdir(parents=True, exist_ok=True)
        outcome(env.step)
        return 0

    raw = Doltlite(raw_db)
    # Pull the cheap change-signal for EVERY document in two queries, so an
    # unchanged re-render never reads a single 250KB payload. Without this a
    # no-op run still costs ~1100 subprocess round-trips and five minutes.
    sigs = {r["id"]: (r.get("source_sha") or "") for r in raw.query_json(
        "SELECT t.id AS id, b.source_sha AS source_sha FROM transcripts t "
        "LEFT JOIN transcripts_bookkeeping b ON t.id = b.id ORDER BY t.id;")}
    media_by_id: dict[str, list] = {}
    for r in raw.query_json(
            "SELECT transcript_id, field, rel_path FROM media ORDER BY transcript_id, field;"):
        media_by_id.setdefault(r["transcript_id"], []).append(
            {"field": r["field"], "rel_path": r["rel_path"]})
    ids = list(sigs)
    if not ids:
        log("raw store holds no transcripts", "info")
        out_dir.mkdir(parents=True, exist_ok=True)
        outcome(env.step)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    db = Doltlite(out_dir / STORE)
    db.script(DDL)

    ist = identity_stats()
    log(f"identity: {ist['confirmed']} confirmed name(s) covering "
        f"{ist['variants']} variant(s); {ist['proposed']} proposed and NOT applied")

    prior = {}
    try:
        for r in db.query_json(
                "SELECT markdown_uuid, source_fingerprint, row_set_hash FROM markdowns;"):
            prior[r["markdown_uuid"]] = (r.get("source_fingerprint"), r.get("row_set_hash"))
    except RuntimeError:
        prior = {}

    progress_length(len(ids))
    stmts: list[str] = []
    rendered = unchanged = 0
    total_rows = 0

    for n, tid in enumerate(ids, 1):
        media = media_by_id.get(tid, [])
        # Derived from the ingest step's own source hash, not the payload, so
        # this is known before any expensive read.
        fingerprint = hashlib.sha256(
            (sigs[tid] + json.dumps(media, sort_keys=True)
             + RENDER_SIG).encode()).hexdigest()

        # ONE constant feeds both qmd_path and md_path. They must be
        # byte-equal or the row is dropped from free-text search silently.
        qmd_path = f"{env.step}/{tid}/all.md"

        # Unchanged fingerprint AND the document still on disk: skip without
        # reading the payload at all.
        if (prior.get(tid, (None, None))[0] == fingerprint
                and (out_dir / tid / "all.md").is_file()):
            unchanged += 1
            progress_inc()
            continue

        got = raw.query_json(
            f"SELECT payload FROM transcripts WHERE id = {sql_str(tid)};")
        if not got:
            progress_inc()
            continue
        t = json.loads(got[0]["payload"])

        md = render_markdown(t, media)
        rows = grid_rows_for(t, qmd_path, tid, len(md.encode()))
        row_hash = hashlib.sha256(
            json.dumps([[r.get(c) for c in COLS] for r in rows],
                       sort_keys=True, default=str).encode()).hexdigest()

        doc_dir = out_dir / tid
        doc_dir.mkdir(parents=True, exist_ok=True)
        (doc_dir / "all.md").write_text(md)

        stmts.append(f"DELETE FROM grid_rows WHERE conversation_uuid = {sql_str(tid)}")
        stmts.extend(row_sql(r) for r in rows)
        stmts.append(
            "INSERT INTO markdowns (markdown_uuid, source_name, provider, kind, title, "
            "created_at, updated_at, md_path, source_fingerprint, row_set_hash, "
            "renderer_version, rendered_at) VALUES ("
            f"{sql_str(tid)}, {sql_str(PROVIDER)}, {sql_str(PROVIDER)}, "
            f"{sql_str(KIND_MEETING)}, {sql_str(t.get('title'))}, "
            f"{sql_str(iso_utc(t.get('date') or 0))}, {sql_str(iso_utc(t.get('date') or 0))}, "
            f"{sql_str(qmd_path)}, {sql_str(fingerprint)}, {sql_str(row_hash)}, "
            f"{sql_str(RENDERER_VERSION)}, {sql_str(env.now)}) "
            "ON CONFLICT(markdown_uuid) DO UPDATE SET title=excluded.title, "
            "md_path=excluded.md_path, source_fingerprint=excluded.source_fingerprint, "
            "row_set_hash=excluded.row_set_hash, rendered_at=excluded.rendered_at")

        rendered += 1
        total_rows += len(rows)
        progress_inc()

        if len(stmts) >= BATCH:
            db.script(stmts)
            stmts = []
            progress_message(f"rendered {n}/{len(ids)}")

    if stmts:
        db.script(stmts)

    head = db.commit(f"fireflies render: {rendered} documents, {total_rows} rows")

    # Prove the thing grid_index actually reads exists and has rows in it.
    # Writing markdown and no store is the classic silent success here:
    # every step reports done and the source contributes NOTHING to the
    # index, with no error anywhere to explain it. Better to fail the step.
    store = out_dir / STORE
    if not store.is_file():
        log(f"render wrote no {STORE} -- grid_index would skip this source "
            f"silently. Expected it at {store}", "error")
        outcome(env.step, failure="data")
        return 1
    try:
        n_rows = int(db.query("SELECT count(*) FROM grid_rows;")[0][0])
    except (RuntimeError, IndexError, ValueError) as e:
        log(f"wrote {STORE} but could not read it back: {e}", "error")
        outcome(env.step, failure="data")
        return 1
    if n_rows == 0 and ids:
        log(f"{len(ids)} transcripts in the raw store but 0 rows in {STORE} -- "
            "the source would appear empty in the index", "error")
        outcome(env.step, failure="data")
        return 1

    # No render_sig in the config means a change to THIS file will not make
    # datalib re-run the step: it fingerprints the config entry and the
    # inputs, never the script's bytes. The step would be skipped while
    # reporting success, and the old output would stand.
    if not (env.params or {}).get("render_sig"):
        log("no `render_sig` in [steps.params]: editing this renderer will "
            "NOT trigger a re-render, because datalib fingerprints the config "
            "and inputs, not the script. Add one and bump it on render "
            "changes -- see docs/DATALIB-SOURCE.md", "warn")

    log(f"{rendered} rendered ({total_rows} grid rows), {unchanged} unchanged; "
        f"{STORE} holds {n_rows} rows", "info")
    outcome(env.step, head)
    return 0


if __name__ == "__main__":
    sys.exit(main())
