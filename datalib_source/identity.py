#!/usr/bin/env python3
"""Speaker identity: collapse spellings of one person to a canonical name.

Applied at RENDER time by each source's render step. Two places it must
NOT be applied, both for the same reason -- they are not where the truth
lives:

  the dump      immutable; the service it came from may be gone
  grid_rows     derived; the next grid_index run rebuilds it from the
                render stores and any UPDATE vanishes without a trace

So the renderer is the one honest place: change it, re-run the sync, and
every derived artifact agrees.

Only the `confirmed` block in aliases.json is applied. Everything under
`proposed` is what the data suggests and a human has not yet ratified --
the same contract as inference.confirmed elsewhere in this repo. Guessing
here is how two people quietly become one.

Usage from a step:

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "identity"))
    from identity import canonical
    name = canonical(raw_speaker_name)
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

ALIASES_PATH = Path(__file__).resolve().parent / "aliases.json"

_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    """Lookup key: case- and whitespace-insensitive."""
    return _WS.sub(" ", str(s or "").strip()).casefold()


@lru_cache(maxsize=1)
def _table() -> dict[str, str]:
    """variant-key -> canonical name, from the confirmed block only."""
    try:
        doc = json.loads(ALIASES_PATH.read_text())
    except (OSError, ValueError):
        # A missing or broken alias file must not take the render down --
        # names simply pass through unchanged, which is the old behaviour.
        return {}
    out: dict[str, str] = {}
    for canon, entry in (doc.get("confirmed") or {}).items():
        variants = entry.get("variants", []) if isinstance(entry, dict) else entry
        # The canonical spelling maps to itself so casing is normalised too.
        out[_norm(canon)] = canon
        for v in variants:
            out[_norm(v)] = canon
    return out


def canonical(name: str | None) -> str:
    """The canonical spelling of `name`, or `name` unchanged."""
    if not name:
        return name or ""
    return _table().get(_norm(name), str(name).strip())


def stats() -> dict:
    """What the table covers -- for a step to log on startup."""
    try:
        doc = json.loads(ALIASES_PATH.read_text())
    except (OSError, ValueError):
        return {"confirmed": 0, "variants": 0, "proposed": 0}
    conf = doc.get("confirmed") or {}
    return {
        "confirmed": len(conf),
        "variants": sum(len(e.get("variants", [])) for e in conf.values()
                        if isinstance(e, dict)),
        "proposed": len(doc.get("proposed") or {}),
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for a in sys.argv[1:]:
            print(f"{a!r} -> {canonical(a)!r}")
    else:
        print(json.dumps(stats(), indent=1))
