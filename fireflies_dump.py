#!/usr/bin/env python3
"""fireflies_dump.py -- liberate a Fireflies.ai account into a local dump.

Modes
  introspect  one request; print the real shape of the API types, so the
              full transcript query is built from the schema, not memory
  inventory   page the transcripts list -> inventory.json + stats
  dump        per transcript: full JSON + signed media, resumable

The API key is read from $FIREFLIES_API_KEY. It is never written to a
file, a log line, or the dump. Nothing leaves this machine except to
api.fireflies.ai and the signed media host it hands back.

Rate limits (docs.fireflies.ai): Free 50/day, Pro 500/day,
Business/Enterprise 60/min. One transcript == one request.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

API = "https://api.fireflies.ai/graphql"
DEFAULT_OUT = "~/fireflies-dump"
PAGE = 50          # hard API maximum for transcripts(limit:)
MAX_RETRY = 6
MEDIA_FIELDS = ("audio_url", "video_url", "transcript_url")

# --- the queries -----------------------------------------------------------

Q_INVENTORY = """
query Inventory($limit: Int, $skip: Int, $fromDate: DateTime, $toDate: DateTime) {
  transcripts(limit: $limit, skip: $skip, fromDate: $fromDate, toDate: $toDate) {
    id
    title
    date
    duration
    host_email
    organizer_email
  }
}
"""

# Field set confirmed against docs.fireflies.ai. Sub-selections for
# meeting_attendees / summary are verified by `introspect` before first use.
Q_TRANSCRIPT = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    dateString
    duration
    privacy
    meeting_link
    is_live
    transcript_url
    audio_url
    video_url
    host_email
    organizer_email
    participants
    fireflies_users
    workspace_users
    calendar_id
    cal_id
    calendar_type
    user { user_id email name }
    speakers { id name }
    meeting_attendees { displayName email phoneNumber name location }
    meeting_attendance { name join_time leave_time }
    meeting_info { silent_meeting summary_status fred_joined }
    sentences {
      index
      speaker_name
      speaker_id
      text
      raw_text
      start_time
      end_time
      ai_filters { text_cleanup task pricing metric question date_and_time sentiment }
    }
    summary {
      overview
      short_overview
      short_summary
      gist
      bullet_gist
      shorthand_bullet
      outline
      notes
      action_items
      keywords
      topics_discussed
      transcript_chapters
      meeting_type
      extended_sections { title content }
    }
    analytics {
      sentiments { negative_pct neutral_pct positive_pct }
      categories { questions date_times metrics tasks }
      speakers {
        speaker_id name duration word_count longest_monologue
        monologues_count filler_words questions duration_pct words_per_minute
      }
    }
  }
}
"""

# Fireflies declares MeetingAttendance.join_time non-nullable but returns
# null for it on some meetings, which fails the WHOLE query. This drops
# only that block so the rest of the meeting is still captured.
Q_TRANSCRIPT_NO_ATTENDANCE = """
query Transcript($id: String!) {
  transcript(id: $id) {
    id
    title
    date
    dateString
    duration
    privacy
    meeting_link
    is_live
    transcript_url
    audio_url
    video_url
    host_email
    organizer_email
    participants
    fireflies_users
    workspace_users
    calendar_id
    cal_id
    calendar_type
    user { user_id email name }
    speakers { id name }
    meeting_attendees { displayName email phoneNumber name location }
    meeting_info { silent_meeting summary_status fred_joined }
    sentences {
      index
      speaker_name
      speaker_id
      text
      raw_text
      start_time
      end_time
      ai_filters { text_cleanup task pricing metric question date_and_time sentiment }
    }
    summary {
      overview
      short_overview
      short_summary
      gist
      bullet_gist
      shorthand_bullet
      outline
      notes
      action_items
      keywords
      topics_discussed
      transcript_chapters
      meeting_type
      extended_sections { title content }
    }
    analytics {
      sentiments { negative_pct neutral_pct positive_pct }
      categories { questions date_times metrics tasks }
      speakers {
        speaker_id name duration word_count longest_monologue
        monologues_count filler_words questions duration_pct words_per_minute
      }
    }
  }
}
"""

Q_INTROSPECT = """
query Introspect($name: String!) {
  __type(name: $name) {
    name
    kind
    fields {
      name
      type { kind name ofType { kind name ofType { kind name } } }
    }
  }
}
"""

Q_WHOAMI = "{ user { name email } }"


# --- plumbing --------------------------------------------------------------

class Fireflies:
    """Thin GraphQL client: paces itself, backs off on 429, counts calls."""

    def __init__(self, key: str, rpm: int, verbose: bool = False):
        self._key = key
        self.min_interval = 60.0 / max(rpm, 1)
        self.calls = 0
        self.verbose = verbose
        self._last = 0.0
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })

    def _pace(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def query(self, query: str, variables: dict | None = None) -> dict:
        """POST a query. Raises RuntimeError on a hard failure."""
        payload = {"query": query, "variables": variables or {}}
        delay = 5.0
        for attempt in range(1, MAX_RETRY + 1):
            self._pace()
            try:
                r = self._s.post(API, json=payload, timeout=120)
            except requests.RequestException as e:
                if attempt == MAX_RETRY:
                    raise RuntimeError(f"network: {e}") from e
                warn(f"network error ({e.__class__.__name__}), retry {attempt}/{MAX_RETRY} in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue

            self.calls += 1

            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
                nap = float(retry_after) if (retry_after or "").strip().isdigit() else delay
                warn(f"429 rate limited; sleeping {nap:.0f}s (attempt {attempt}/{MAX_RETRY})")
                time.sleep(nap)
                delay = min(delay * 2, 300)
                continue

            if r.status_code in (401, 403):
                raise RuntimeError(f"auth: HTTP {r.status_code} -- check FIREFLIES_API_KEY")

            if r.status_code >= 500:
                if attempt == MAX_RETRY:
                    raise RuntimeError(f"server: HTTP {r.status_code}")
                warn(f"HTTP {r.status_code}, retry {attempt}/{MAX_RETRY} in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue

            try:
                body = r.json()
            except ValueError as e:
                raise RuntimeError(f"non-JSON response (HTTP {r.status_code})") from e

            if body.get("errors"):
                msg = "; ".join(e.get("message", "?") for e in body["errors"])
                # Fireflies reports throttling inside a 200 body too.
                if "rate" in msg.lower() or "limit" in msg.lower():
                    warn(f"rate limited (in body): {msg}; sleeping {delay:.0f}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 300)
                    continue
                raise RuntimeError(f"graphql: {msg}")

            return body.get("data") or {}

        raise RuntimeError("exhausted retries")

    def size(self, url: str) -> tuple[int, str]:
        """Content-Length without downloading. Falls back to a 1-byte range
        GET when the signed URL refuses HEAD. Returns (bytes, content_type)."""
        r = self._s.head(url, timeout=60, allow_redirects=True,
                         headers={"Authorization": None})
        if r.status_code < 400 and r.headers.get("Content-Length"):
            return int(r.headers["Content-Length"]), r.headers.get("Content-Type", "")
        r = self._s.get(url, timeout=60, stream=True,
                        headers={"Authorization": None, "Range": "bytes=0-0"})
        r.close()
        rng = r.headers.get("Content-Range", "")      # e.g. "bytes 0-0/12345678"
        if "/" in rng:
            return int(rng.rsplit("/", 1)[1]), r.headers.get("Content-Type", "")
        return -1, r.headers.get("Content-Type", "")

    def download(self, url: str, dest: Path) -> int:
        """Stream a signed URL to dest. Returns bytes written."""
        with self._s.get(url, stream=True, timeout=300, headers={"Authorization": None}) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            n = 0
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
                        n += len(chunk)
            tmp.replace(dest)          # atomic: no torn file on resume
            return n


def warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr, flush=True)


def resolve_key(service: str) -> str:
    """Find the API key without ever putting it on a command line.

    Order: $FIREFLIES_API_KEY, then the macOS login Keychain. The key is
    returned to the caller and nowhere else -- never logged, never written.
    """
    env = os.environ.get("FIREFLIES_API_KEY", "").strip()
    if env:
        return env
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["security", "find-generic-password",
                 "-a", os.environ.get("USER", ""), "-s", service, "-w"],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    # Last resort: ask. getpass does not echo and the value never reaches
    # the environment, the shell history, or the process list -- which is
    # more than can be said for the alternatives. Only when someone is
    # actually at a terminal; a cron run must fail with the message below
    # rather than hang on a prompt nobody will answer.
    if sys.stdin.isatty() and sys.stderr.isatty():
        try:
            typed = getpass.getpass("Fireflies API key (input hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return ""
        if typed:
            print("Tip: store it so you are not asked again --\n"
                  f'  security add-generic-password -a "$USER" -s {service} '
                  '-w "$(pbpaste)"' if sys.platform == "darwin" else
                  "Tip: export FIREFLIES_API_KEY to avoid the prompt.",
                  file=sys.stderr)
            return typed
    return ""


def iso(day: str | None, end: bool = False) -> str | None:
    """YYYY-MM-DD -> ISO 8601 instant the API accepts."""
    if not day:
        return None
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end:
        d = d.replace(hour=23, minute=59, second=59)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def when(value) -> datetime | None:
    """The API's `date` is epoch-millis on some plans, ISO on others."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def ext_for(url: str, content_type: str | None = None) -> str:
    suffix = Path(urlparse(url).path).suffix
    if suffix and len(suffix) <= 6:
        return suffix
    return {
        "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/wav": ".wav",
        "video/mp4": ".mp4", "video/webm": ".webm",
        "application/json": ".json", "text/html": ".html", "text/plain": ".txt",
    }.get((content_type or "").split(";")[0].strip(), ".bin")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


# --- modes -----------------------------------------------------------------

def mode_introspect(ff: Fireflies, args) -> int:
    """One request per type. Prints the real schema so queries aren't guesses."""
    for name in args.types:
        data = ff.query(Q_INTROSPECT, {"name": name})
        t = data.get("__type")
        if not t:
            print(f"\n{name}: NOT FOUND in schema")
            continue
        print(f"\n=== {t['name']} ({t['kind']}) ===")
        for f in t.get("fields") or []:
            ty = f["type"]
            parts = []
            while ty:
                if ty.get("name"):
                    parts.append(ty["name"])
                if ty.get("kind") == "LIST":
                    parts.append("[]")
                ty = ty.get("ofType")
            print(f"  {f['name']:<28} {' '.join(parts) or '?'}")
    print(f"\n[{ff.calls} request(s) used]")
    return 0


def mode_inventory(ff: Fireflies, args) -> int:
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    variables = {"limit": PAGE, "fromDate": iso(args.since), "toDate": iso(args.until, end=True)}

    if args.dry_run:
        print("DRY RUN -- would page transcripts with:")
        print(json.dumps({**variables, "skip": "0, 50, 100, ..."}, indent=2))
        return 0

    rows: list[dict] = []
    skip = 0
    while True:
        data = ff.query(Q_INVENTORY, {**variables, "skip": skip})
        page = data.get("transcripts") or []
        rows.extend(page)
        print(f"  page @ skip={skip}: {len(page)} (total {len(rows)})", flush=True)
        if len(page) < PAGE:
            break
        skip += PAGE
        if args.max and len(rows) >= args.max:
            break

    if args.max:
        rows = rows[: args.max]

    # stats
    seen: dict[str, int] = {}
    for r in rows:
        seen[r["id"]] = seen.get(r["id"], 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    dates = sorted(d for d in (when(r.get("date")) for r in rows) if d)
    hours = sum(float(r.get("duration") or 0) for r in rows)
    # `duration` is minutes on current plans; report both readings rather
    # than silently picking one.
    payload = {
        "pulled_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "filters": {"since": args.since, "until": args.until},
        "transcripts": rows,
    }
    (out / "inventory.json").write_text(json.dumps(payload, indent=2))

    print()
    print(f"  total meetings   {len(rows)}")
    print(f"  earliest         {dates[0].date().isoformat() if dates else '-'}")
    print(f"  latest           {dates[-1].date().isoformat() if dates else '-'}")
    print(f"  total duration   {hours/60:.1f}h  (if duration is minutes)")
    print(f"                   {hours:.1f}h  (if duration is hours)")
    print(f"  duplicate ids    {len(dupes)}{' ' + str(list(dupes)[:5]) if dupes else ''}")
    print(f"  requests used    {ff.calls}")
    print(f"  written          {out/'inventory.json'}")
    print()
    print(f"  next: ~{len(rows)} requests for the full dump"
          f" (~{len(rows)*ff.min_interval/60:.0f} min at this pace)")
    return 0


def load_manifest(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def is_complete(out: Path, tid: str, want: tuple = ()) -> bool:
    """Complete == JSON on disk AND every requested media file settled ok.

    `want` is the media fields asked for this run: a dump that fetched
    audio-only is NOT complete for a later run that also wants video.
    """
    j = out / "transcripts" / f"{tid}.json"
    if not (j.is_file() and j.stat().st_size > 0):
        return False
    man = load_manifest(out / "media" / tid / "_manifest.json")
    if man is None:
        return False
    files = man.get("files", [])
    if any(f.get("status") != "ok" for f in files):
        return False
    settled = {f["field"] for f in files} | set(man.get("absent", []))
    return all(w in settled for w in want)


def mode_dump(ff: Fireflies, args) -> int:
    out = args.out
    inv_path = out / "inventory.json"
    if not inv_path.is_file():
        print(f"no inventory at {inv_path} -- run inventory mode first", file=sys.stderr)
        return 2

    rows = json.loads(inv_path.read_text())["transcripts"]
    if args.since or args.until:
        lo = when(iso(args.since)) if args.since else None
        hi = when(iso(args.until, end=True)) if args.until else None
        rows = [r for r in rows
                if (d := when(r.get("date"))) is None
                or ((lo is None or d >= lo) and (hi is None or d <= hi))]
    if args.max:
        rows = rows[: args.max]

    todo = [r for r in rows if not is_complete(out, r["id"], args.media_fields)]
    done_already = len(rows) - len(todo)
    print(f"  {len(rows)} in scope | {done_already} already complete | {len(todo)} to fetch")

    if args.dry_run:
        print("DRY RUN -- no requests made. Would fetch:")
        for r in todo[:20]:
            print(f"    {r['id']}  {str(r.get('title'))[:60]}")
        if len(todo) > 20:
            print(f"    ... and {len(todo)-20} more")
        return 0
    if not todo:
        print("  nothing to do")
        return 0

    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    progress = (out / "progress.jsonl").open("a")
    started = time.monotonic()
    ok = json_err = media_err = 0
    total_bytes = 0

    for i, row in enumerate(todo, 1):
        tid = row["id"]
        entry = {"id": tid, "ts": datetime.now(timezone.utc).isoformat(),
                 "title": row.get("title"), "status": "ok",
                 "bytes": 0, "media_errors": []}
        try:
            try:
                data = ff.query(Q_TRANSCRIPT, {"id": tid})
            except RuntimeError as e:
                # Their schema says non-nullable; their data disagrees.
                if "non-nullable" in str(e) and "MeetingAttendance" in str(e):
                    warn(f"{tid}: dropping meeting_attendance (upstream null)")
                    data = ff.query(Q_TRANSCRIPT_NO_ATTENDANCE, {"id": tid})
                    entry["degraded"] = "meeting_attendance omitted (upstream null)"
                else:
                    raise
            tr = data.get("transcript")
            if not tr:
                raise RuntimeError("transcript returned null")
            # raw, unmodified
            (out / "transcripts" / f"{tid}.json").write_text(json.dumps(tr, indent=2))
        except RuntimeError as e:
            if str(e).startswith("auth:"):
                print(f"\nFATAL {e}", file=sys.stderr)
                progress.close()
                return 3
            entry.update(status="json_error", error=str(e))
            json_err += 1
            progress.write(json.dumps(entry) + "\n"); progress.flush()
            warn(f"{tid}: {e}")
            continue

        # Media URLs are signed and short-lived: fetch NOW, not in a later pass.
        files, absent = [], []
        for field in args.media_fields:
            url = tr.get(field)
            if not url:
                absent.append(field)
                continue
            dest_dir = out / "media" / tid
            dest = dest_dir / f"{field}{ext_for(url)}"
            try:
                n = ff.download(url, dest)
                files.append({"field": field, "file": dest.name, "bytes": n, "status": "ok"})
                entry["bytes"] += n
                total_bytes += n
            except Exception as e:                      # media failure != json failure
                files.append({"field": field, "file": dest.name, "bytes": 0,
                              "status": "error", "error": str(e)})
                entry["media_errors"].append({"field": field, "error": str(e)})
                warn(f"{tid}: media {field}: {e}")

        media_dir = out / "media" / tid
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "_manifest.json").write_text(json.dumps(
            {"id": tid, "fetched_at": entry["ts"], "files": files,
             "absent": absent}, indent=2))

        if entry["media_errors"]:
            media_err += 1
        else:
            ok += 1
        progress.write(json.dumps(entry) + "\n"); progress.flush()

        if i % 10 == 0 or i == len(todo):
            rate = (time.monotonic() - started) / i
            eta = rate * (len(todo) - i)
            print(f"  [{i}/{len(todo)}] ok={ok} json_err={json_err} media_err={media_err} "
                  f"{human(total_bytes)} | ETA {eta/60:.1f}m", flush=True)

    progress.close()
    print(f"\n  complete={ok} json_errors={json_err} media_errors={media_err} "
          f"bytes={human(total_bytes)} requests={ff.calls}")
    return 0


def mode_probe(ff: Fireflies, args) -> int:
    """Fetch a few transcripts and MEASURE their media without downloading."""
    out = args.out
    inv = json.loads((out / "inventory.json").read_text())
    rows = inv["transcripts"]
    n = args.max or 3

    # Sample across the range rather than taking the newest N, so the size
    # projection isn't skewed by one era's recording settings.
    step = max(len(rows) // n, 1)
    sample = [rows[i * step] for i in range(n)]

    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    measured, report = [], []
    for row in sample:
        tid = row["id"]
        tr = ff.query(Q_TRANSCRIPT, {"id": tid}).get("transcript")
        if not tr:
            warn(f"{tid}: transcript returned null")
            continue
        (out / "transcripts" / f"{tid}.json").write_text(json.dumps(tr, indent=2))
        item = {"id": tid, "title": tr.get("title"), "duration_min": tr.get("duration"),
                "sentences": len(tr.get("sentences") or []),
                "has_summary": bool((tr.get("summary") or {}).get("overview")),
                "has_analytics": bool(tr.get("analytics")), "media": {}}
        for field in MEDIA_FIELDS:
            url = tr.get(field)
            if not url:
                item["media"][field] = None
                continue
            try:
                nbytes, ctype = ff.size(url)
                item["media"][field] = {"bytes": nbytes, "content_type": ctype}
                if nbytes > 0 and (tr.get("duration") or 0) > 0:
                    measured.append((field, nbytes, float(tr["duration"])))
            except Exception as e:
                item["media"][field] = {"error": str(e)}
        report.append(item)

    print(json.dumps(report, indent=2))

    # project across the whole library
    total_min = sum(float(r.get("duration") or 0) for r in rows)
    print(f"\n--- projection over {len(rows)} meetings / {total_min/60:.0f}h ---")
    per_field = {}
    for field, nbytes, mins in measured:
        per_field.setdefault(field, []).append(nbytes / mins)   # bytes per minute
    grand = 0.0
    for field, rates in per_field.items():
        avg = sum(rates) / len(rates)
        projected = avg * total_min
        grand += projected
        print(f"  {field:<16} {human(avg*60)}/hour  ->  {human(projected)} total")
    print(f"  {'ALL':<16} {'':>14}      {human(grand)} total")
    print(f"\n[{ff.calls} request(s) used]")
    return 0


def mode_verify(ff, args) -> int:
    """A3: reconcile inventory.json against what is actually on disk."""
    out = args.out
    inv = json.loads((out / "inventory.json").read_text())
    rows = inv["transcripts"]

    complete, json_only, missing, degraded = [], [], [], []
    media_errors, json_errors = [], []
    tbytes = mbytes = 0
    turns_total = sentences_total = 0
    no_speech = anon = named = 0

    for r in rows:
        tid = r["id"]
        j = out / "transcripts" / f"{tid}.json"
        if not (j.is_file() and j.stat().st_size > 0):
            missing.append((tid, r.get("title")))
            continue
        tbytes += j.stat().st_size
        try:
            t = json.loads(j.read_text())
        except ValueError as e:
            json_errors.append((tid, r.get("title"), f"unparseable on disk: {e}"))
            continue
        sents = t.get("sentences") or []
        sentences_total += len(sents)
        if not sents:
            no_speech += 1
        elif t.get("speakers"):
            named += 1
        else:
            anon += 1

        man = load_manifest(out / "media" / tid / "_manifest.json")
        if man is None:
            json_only.append((tid, r.get("title")))
            continue
        files = man.get("files", [])
        bad = [f for f in files if f.get("status") != "ok"]
        for f in bad:
            media_errors.append((tid, f.get("field"), f.get("error", "?")))
        mbytes += sum(int(f.get("bytes") or 0) for f in files if f.get("status") == "ok")
        if files:
            complete.append(tid)
        else:
            json_only.append((tid, r.get("title")))

    # errors recorded during the run
    prog = out / "progress.jsonl"
    if prog.is_file():
        for line in prog.read_text().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("degraded"):
                # progress.jsonl is append-only, so a retried transcript has
                # more than one line. Report the record once, not per attempt.
                if d["id"] not in {x[0] for x in degraded}:
                    degraded.append((d["id"], d["degraded"]))
            if d.get("status") == "json_error":
                # only report it if it is STILL missing
                if not (out / "transcripts" / f"{d['id']}.json").is_file():
                    json_errors.append((d["id"], d.get("title"), d.get("error")))

    print(f"inventory      {len(rows)} meetings")
    print(f"  complete     {len(complete):>5}  (json + at least one media file)")
    print(f"  json only    {len(json_only):>5}  (no media fetched yet)")
    print(f"  missing      {len(missing):>5}")
    print(f"\ndisk")
    print(f"  transcripts  {human(tbytes)}")
    print(f"  media        {human(mbytes)}")
    print(f"  total        {human(tbytes + mbytes)}")
    print(f"\ncontent")
    print(f"  sentences    {sentences_total:,}")
    print(f"  named speakers   {named:>5}")
    print(f"  anonymous        {anon:>5}  (Speaker 1/2/3 - uploaded audio)")
    print(f"  no speech        {no_speech:>5}  (no-show)")

    if degraded:
        print(f"\ndegraded ({len(degraded)}) - captured, with one field dropped:")
        for tid, why in degraded:
            print(f"  {tid}  {why}")
    print(f"\njson errors ({len(json_errors)})")
    for tid, title, err in json_errors:
        print(f"  {tid}  {str(title)[:36]}  {err}")
    print(f"media errors ({len(media_errors)})")
    for tid, field, err in media_errors[:20]:
        print(f"  {tid}  {field}  {err}")
    if len(media_errors) > 20:
        print(f"  ... and {len(media_errors) - 20} more")

    return 0 if not (missing or json_errors) else 1


# --- entry -----------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["introspect", "inventory", "probe", "dump", "verify", "whoami"])
    p.add_argument("--out", default=DEFAULT_OUT, help=f"dump directory (default {DEFAULT_OUT})")
    p.add_argument("--since", metavar="YYYY-MM-DD")
    p.add_argument("--until", metavar="YYYY-MM-DD")
    p.add_argument("--max", type=int, default=0, help="cap items (for a trial run)")
    p.add_argument("--rpm", type=int, default=60, help="requests/min ceiling (default 60)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--types", nargs="*", default=["Transcript", "Summary", "Sentence", "User"],
                   help="introspect: type names")
    p.add_argument("--keychain-service", default="FIREFLIES_API_KEY",
                   help="macOS Keychain service name holding the key")
    p.add_argument("--media", default="audio,video",
                   help="which media to download: comma list of audio,video,transcript,none "
                        "(default audio,video; 'transcript' is the Fireflies web page, not a file)")
    args = p.parse_args()
    args.out = Path(os.path.expanduser(args.out))

    key = resolve_key(args.keychain_service)
    if not key and not args.dry_run and args.mode != "verify":
        print(
            "No API key found. Either:\n"
            "  1. store it in the Keychain (recommended) -- copy the key from\n"
            "     https://app.fireflies.ai/integrations/custom/fireflies then run:\n"
            f'       security add-generic-password -a "$USER" -s {args.keychain_service} -w "$(pbpaste)"\n'
            "  2. or set FIREFLIES_API_KEY in the environment.",
            file=sys.stderr)
        return 2

    picked = {t.strip() for t in args.media.split(",") if t.strip()}
    if "none" in picked:
        args.media_fields = ()
    else:
        args.media_fields = tuple(f"{n}_url" for n in ("audio", "video", "transcript")
                                  if n in picked)

    ff = Fireflies(key, rpm=args.rpm)
    try:
        if args.mode == "whoami":
            print(json.dumps(ff.query(Q_WHOAMI), indent=2))
            return 0
        if args.mode == "introspect":
            return mode_introspect(ff, args)
        if args.mode == "inventory":
            return mode_inventory(ff, args)
        if args.mode == "probe":
            return mode_probe(ff, args)
        if args.mode == "verify":
            return mode_verify(ff, args)
        return mode_dump(ff, args)
    except KeyboardInterrupt:
        print("\ninterrupted -- re-run to resume", file=sys.stderr)
        return 130
    except RuntimeError as e:
        print(f"\nfailed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
