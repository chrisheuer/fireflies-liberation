"""Shared plumbing for the fireflies datalib steps.

Stdlib only: these run as DAG steps under whatever `python3` the runner
finds, not under this repo's venv.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DOLTLITE = os.environ.get("DATALIB_DOLTLITE", "datalib-doltlite")


# --- the NDJSON event protocol (docs/dev/step_protocol.md) -----------------

def emit(obj: dict) -> None:
    """One NDJSON event on stdout. The runner re-tags `step` itself."""
    print(json.dumps(obj), flush=True)


def log(msg: str, level: str = "info") -> None:
    emit({"event": "log", "step": "", "level": level, "msg": msg})


def progress_length(total: int) -> None:
    emit({"event": "progress_length", "step": "", "total": total})


def progress_inc(delta: int = 1) -> None:
    emit({"event": "progress_inc", "step": "", "delta": delta})


def progress_message(msg: str) -> None:
    emit({"event": "progress_message", "step": "", "msg": msg})


def outcome(path: str, version: str | None = None, failure: str | None = None) -> None:
    """The last line. `version` MUST be a function of output content.

    Omitting it is correct but slower -- the scheduler then blake3-hashes
    the whole tree. We always have doltlite's commit hash, so we use it.
    """
    out = {"path": path}
    if version:
        out["version"] = version
    ev = {"event": "outcome", "outputs": [out]}
    if failure:
        ev["failure"] = failure
    emit(ev)


# --- the environment the runner hands us -----------------------------------

class StepEnv:
    def __init__(self) -> None:
        self.step = os.environ["DATALIB_DAG_STEP"]          # the one tree we write
        self.data_root = Path(os.environ.get("DATALIB_DAG_DATA_ROOT", os.getcwd()))
        self.now = os.environ.get("DATALIB_DAG_NOW", "")
        self.inputs = [p for p in os.environ.get("DATALIB_DAG_INPUTS", "").split("\n") if p]
        self.changed = [p for p in os.environ.get("DATALIB_DAG_CHANGED_INPUTS", "").split("\n") if p]
        self.reset = os.environ.get("DATALIB_DAG_RESET_AND_REDOWNLOAD", "") == "1"
        argv = sys.argv[1:]
        flags = dict(zip(argv[0::2], argv[1::2]))
        self.params = json.loads(flags.get("--params", "{}"))

    @property
    def out_dir(self) -> Path:
        return self.data_root / self.step


# --- doltlite via the CLI ---------------------------------------------------

class Doltlite:
    """A doltlite store driven through `datalib-doltlite` (sqlite3 argv).

    We shell out rather than bind a library: the binary ships in the same
    release as datalib-dag and is already on PATH.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _run(self, sql: str, readonly: bool = False) -> str:
        """SQL goes in on stdin, never argv.

        Transcript payloads run to ~250KB each and ARG_MAX is 1MiB, so a
        batched INSERT as a command-line argument fails outright. doltlite
        reads a script from stdin when given no SQL argument.
        """
        cmd = [DOLTLITE]
        if readonly:
            cmd.append("-readonly")
        cmd.append(str(self.path))
        p = subprocess.run(cmd, input=sql, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"doltlite: {p.stderr.strip() or p.stdout.strip()}")
        # doltlite writes some notices to stderr even on success; surface them
        err = p.stderr.strip()
        if err and "error" in err.lower():
            raise RuntimeError(f"doltlite: {err}")
        return p.stdout

    def execute(self, sql: str) -> str:
        return self._run(sql)

    def script(self, statements: list[str]) -> None:
        """Many statements in one process -- doltlite charges per invocation."""
        if statements:
            self._run(";\n".join(s.rstrip().rstrip(";") for s in statements) + ";")

    def query(self, sql: str) -> list[list[str]]:
        out = self._run(sql, readonly=True)
        return [line.split("|") for line in out.splitlines() if line]

    def query_json(self, sql: str) -> list[dict]:
        """Rows as dicts. Required for any column that may contain a pipe
        or a newline -- doltlite's default output is pipe-delimited and
        unescaped, so a JSON payload read back that way is unparseable."""
        cmd = [DOLTLITE, "-readonly", "-json", str(self.path)]
        p = subprocess.run(cmd, input=sql, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(f"doltlite: {p.stderr.strip()}")
        out = p.stdout.strip()
        if not out:
            return []
        return json.loads(out)

    def commit(self, message: str) -> str | None:
        """One dolt_commit for the whole run. Returns the hash, or None if
        nothing was staged (doltlite refuses an empty commit)."""
        try:
            out = self._run(f"SELECT dolt_commit('-Am', {sql_str(message)});")
        except RuntimeError as e:
            if "nothing to commit" in str(e).lower() or "no changes" in str(e).lower():
                return self.head()
            raise
        h = out.strip().splitlines()
        return h[-1].strip() if h else None

    def head(self) -> str | None:
        rows = self.query("SELECT commit_hash FROM dolt_log LIMIT 1;")
        return rows[0][0] if rows else None


def sql_str(v) -> str:
    """A SQL string literal. Everything we write goes through here.

    Doubling a single quote is the ONLY escape. doltlite follows SQLite,
    where a backslash inside a string literal is an ordinary character --
    measured, not assumed:

        INSERT ... VALUES ('one\\slash')  -> stored 'one\\slash'  (9 chars)
        INSERT ... VALUES ('back\\\\slash') -> stored 'back\\\\slash' (11 chars)

    So escaping backslashes the MySQL way silently doubles every one of
    them. That is invisible in prose and fatal for a JSON payload:
    json.dumps writes a newline as the two characters `\\n`, so a doubled
    backslash turns it into `\\\\n` and the value no longer parses on the
    way back out.
    """
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"
