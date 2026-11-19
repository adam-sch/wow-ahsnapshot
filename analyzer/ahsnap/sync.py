"""Share snapshots between PCs through a common folder (network share, NAS, synced drive).

Each gaming PC runs `push`, which writes every snapshot in its game file to the shared
folder as its own small file:

    <sync dir>/<Realm>_<Faction>_<epoch>.json.gz

One file per snapshot means PCs never overwrite or conflict with each other, and
re-pushing is harmless (existing files are left alone). The analysis PC runs `import`,
which reads new files from the folder into its local SQLite database.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import socket
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import store

FORMAT = 1
SUFFIX = ".json.gz"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "ahs_config.json"


# --------------------------------------------------------------------------- config

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def sync_dir(override: str | None = None) -> Path | None:
    """Shared folder: --to/--from argument, else AHS_SYNC_DIR, else `ahs.py config --sync-dir`."""
    value = override or os.environ.get("AHS_SYNC_DIR") or load_config().get("sync_dir")
    return Path(value) if value else None


def folder_problem(folder: Path) -> str | None:
    """None if the shared folder can be used, else a short reason it can't."""
    try:
        if folder.is_dir():
            return None
        return "folder not found"
    except OSError as exc:
        if getattr(exc, "winerror", None) in (1326, 5, 86, 1219):
            # bad/missing credentials, access denied, wrong password, conflicting sign-in
            return ("Windows has no working sign-in for this share. Open the share in File Explorer, "
                    "sign in, and tick 'Remember my credentials'.")
        return exc.strerror or str(exc)


# --------------------------------------------------------------------------- push

def snapshot_filename(snap: dict) -> str:
    def clean(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-") or "unknown"
    return f"{clean(snap.get('realm') or '?')}_{clean(snap.get('faction') or 'none')}_{int(snap['t'])}{SUFFIX}"


@dataclass
class PushResult:
    source: Path
    written: int = 0
    already_there: int = 0
    outdated: int = 0
    newest: int | None = None   # epoch of the newest snapshot in the game file


def push_file(saved_vars: Path, dest: Path, machine: str | None = None) -> PushResult:
    snapshots, names = store.read_saved_variables(saved_vars)
    result = PushResult(saved_vars)
    machine = machine or socket.gethostname()
    for snap in snapshots:
        result.newest = max(result.newest or 0, int(snap["t"]))
        if (snap.get("kv") or 1) < store.MIN_KEY_VERSION:
            result.outdated += 1
            continue
        target = dest / snapshot_filename(snap)
        if target.exists():
            result.already_there += 1
            continue
        keys = [str(k) for k in (snap.get("items") or {})]
        payload = {
            "format": FORMAT,
            "machine": machine,
            "account": saved_vars.parent.parent.name,
            "snapshot": snap,
            "names": {k: names[k] for k in keys if k in names},
        }
        # write to a temp name, then rename: a half-copied file is never visible as a snapshot
        tmp = target.with_name(target.name + f".{machine}.tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(tmp, target)
        result.written += 1
    return result


# --------------------------------------------------------------------------- import

SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_files (
    name     TEXT PRIMARY KEY,
    status   TEXT NOT NULL
);
"""


@dataclass
class SyncImportResult:
    folder: Path
    files: int = 0
    new_files: int = 0
    imported: int = 0
    skipped: int = 0     # snapshot already in the database (e.g. imported from the local game file)
    outdated: int = 0
    prices: int = 0
    errors: list[str] = field(default_factory=list)


def import_folder(conn: sqlite3.Connection, folder: Path) -> SyncImportResult:
    conn.executescript(SYNC_SCHEMA)
    result = SyncImportResult(folder)
    seen = {r[0] for r in conn.execute("SELECT name FROM synced_files")}
    for path in sorted(folder.glob("*" + SUFFIX)):
        result.files += 1
        if path.name in seen:
            continue
        result.new_files += 1
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                payload = json.load(f)
            snap = payload["snapshot"]
        except (OSError, ValueError, KeyError) as exc:
            result.errors.append(f"{path.name}: {exc}")  # not recorded, so it's retried next time
            continue
        with conn:
            store.upsert_names(conn, payload.get("names") or {})
            source = f"{payload.get('machine', '?')}:{path.name}"
            status, n = store.import_snapshot(conn, snap, source)
            conn.execute("INSERT INTO synced_files(name, status) VALUES (?, ?)", (path.name, status))
        setattr(result, status, getattr(result, status) + 1)
        result.prices += n
    return result
