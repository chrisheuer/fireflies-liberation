#!/usr/bin/env python3
"""fireflies/ingest -- read the liberation dump into a raw doltlite store.

Reads `params.export.path` (a tools/fireflies_dump.py dump) and writes
`<data_root>/fireflies/ingest/entities.doltlite_db`. No network, no
credentials: the dump is the source of truth.

Per docs/dev/step_protocol.md: a source that was never downloaded is NOT
a failure -- emit nothing and exit 0. Only a store that exists and is
broken is a `data` failure.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (Doltlite, require_doltlite, StepEnv, log, outcome, progress_inc,  # noqa: E402
                     progress_length, progress_message, sql_str)

STORE = "entities.doltlite_db"
BATCH = 20                      # transcripts per doltlite invocation

DDL = [
    # PK is the upstream id as TEXT, per etl/README.md: dolt diff compares
    # rows by PK, so a re-read of the same meeting must land on the same row
    # for "content changed" to mean anything.
    """CREATE TABLE IF NOT EXISTS transcripts (
        id VARCHAR(96) NOT NULL,
        title TEXT,
        date_ms BIGINT,
        duration_min DOUBLE,
        organizer_email VARCHAR(255),
        host_email VARCHAR(255),
        speaker_count INT,
        sentence_count INT,
        has_summary BOOLEAN,
        no_speech BOOLEAN,          -- no SENTENCES; an empty speakers roster
                                    -- does not mean nobody spoke
        payload LONGTEXT NOT NULL,
        PRIMARY KEY (id)
    )""",
    # Fetch bookkeeping lives beside the content, never on it, so a re-read
    # does not show up as a content diff.
    """CREATE TABLE IF NOT EXISTS transcripts_bookkeeping (
        id VARCHAR(96) NOT NULL,
        source_sha VARCHAR(64),
        ingested_at VARCHAR(40),
        PRIMARY KEY (id)
    )""",
    # Media is mirrored with metadata and never rendered -- the media
    # source's download-only pattern. Paths are relative to the dump root.
    """CREATE TABLE IF NOT EXISTS media (
        transcript_id VARCHAR(96) NOT NULL,
        field VARCHAR(32) NOT NULL,
        rel_path VARCHAR(512),
        bytes BIGINT,
        content_type VARCHAR(128),
        status VARCHAR(16),
        PRIMARY KEY (transcript_id, field)
    )""",
]


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
    db_path = out_dir / STORE

    raw = (env.params.get("export") or {}).get("path") or "~/fireflies-dump"
    dump = Path(os.path.expanduser(raw))

    # Absent vs malformed. Never downloaded is the normal state of a
    # freshly-scaffolded source, and failing here would poison the shared
    # index for every source that DID sync.
    tdir = dump / "transcripts"
    if not tdir.is_dir():
        log(f"no dump at {dump} -- nothing to ingest yet", "info")
        out_dir.mkdir(parents=True, exist_ok=True)
        outcome(env.step)
        return 0

    files = sorted(tdir.glob("*.json"))
    if not files:
        log(f"dump at {dump} holds no transcripts", "info")
        out_dir.mkdir(parents=True, exist_ok=True)
        outcome(env.step)
        return 0

    db = Doltlite(db_path)
    db.script(DDL)

    known: dict[str, str] = {}
    if not env.reset:
        try:
            known = {r[0]: r[1] for r in
                     db.query("SELECT id, source_sha FROM transcripts_bookkeeping;")
                     if len(r) >= 2}
        except RuntimeError:
            known = {}
    else:
        log("reset requested -- re-reading the whole dump", "info")

    progress_length(len(files))
    stmts: list[str] = []
    written = skipped = broken = 0

    for i, f in enumerate(files, 1):
        try:
            body = f.read_bytes()
            sha = hashlib.sha256(body).hexdigest()
            tid = f.stem
            if known.get(tid) == sha:
                skipped += 1
                progress_inc()
                continue
            t = json.loads(body)
        except (OSError, ValueError) as e:
            # The file is THERE and won't parse: that is malformed, and the
            # protocol says fail loudly rather than quietly skip.
            log(f"{f.name}: {e}", "error")
            broken += 1
            progress_inc()
            continue

        sents = t.get("sentences") or []
        speakers = t.get("speakers") or []
        summary = t.get("summary") or {}
        stmts.append(
            "INSERT INTO transcripts (id, title, date_ms, duration_min, "
            "organizer_email, host_email, speaker_count, sentence_count, "
            "has_summary, no_speech, payload) VALUES ("
            f"{sql_str(t.get('id') or tid)}, {sql_str(t.get('title'))}, "
            f"{int(t.get('date') or 0)}, {float(t.get('duration') or 0)}, "
            f"{sql_str(t.get('organizer_email'))}, {sql_str(t.get('host_email'))}, "
            f"{len(speakers)}, {len(sents)}, "
            f"{1 if summary.get('overview') else 0}, {1 if not sents else 0}, "
            f"{sql_str(json.dumps(t, separators=(',', ':')))}) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
            "date_ms=excluded.date_ms, duration_min=excluded.duration_min, "
            "organizer_email=excluded.organizer_email, host_email=excluded.host_email, "
            "speaker_count=excluded.speaker_count, sentence_count=excluded.sentence_count, "
            "has_summary=excluded.has_summary, no_speech=excluded.no_speech, "
            "payload=excluded.payload"
        )
        stmts.append(
            "INSERT INTO transcripts_bookkeeping (id, source_sha, ingested_at) VALUES ("
            f"{sql_str(tid)}, {sql_str(sha)}, {sql_str(env.now)}) "
            "ON CONFLICT(id) DO UPDATE SET source_sha=excluded.source_sha, "
            "ingested_at=excluded.ingested_at"
        )

        # Media, if the second pass has run. Absent is normal, not an error.
        man = dump / "media" / tid / "_manifest.json"
        if man.is_file():
            try:
                m = json.loads(man.read_text())
            except ValueError:
                m = {}
            for entry in m.get("files", []):
                stmts.append(
                    "INSERT INTO media (transcript_id, field, rel_path, bytes, "
                    "content_type, status) VALUES ("
                    f"{sql_str(tid)}, {sql_str(entry.get('field'))}, "
                    f"{sql_str('media/' + tid + '/' + str(entry.get('file')))}, "
                    f"{int(entry.get('bytes') or 0)}, "
                    f"{sql_str(entry.get('content_type'))}, {sql_str(entry.get('status'))}) "
                    "ON CONFLICT(transcript_id, field) DO UPDATE SET "
                    "rel_path=excluded.rel_path, bytes=excluded.bytes, "
                    "content_type=excluded.content_type, status=excluded.status"
                )

        written += 1
        progress_inc()

        if len(stmts) >= BATCH * 2:
            db.script(stmts)
            stmts = []
            progress_message(f"ingested {i}/{len(files)}")

    if stmts:
        db.script(stmts)

    # One commit for the whole run; its hash is the content version.
    head = db.commit(f"fireflies ingest: {written} written, {skipped} unchanged")
    log(f"{written} written, {skipped} unchanged, {broken} unreadable", "info")

    if broken and not written:
        outcome(env.step, head, failure="data")
        return 1
    outcome(env.step, head)
    return 0


if __name__ == "__main__":
    sys.exit(main())
