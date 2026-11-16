"""SQLite storage: import AHSnapshot SavedVariables into a durable database.

The game file only keeps the most recent N snapshots (and you can /ahs clear it),
so the database is the long-term history. Importing is idempotent.
"""

from __future__ import annotations

import glob
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import luatable

SAVED_VAR = "AHSnapshot.lua"
SAVED_VAR_GLOBAL = "AHSnapshotDB"

PRICE_FIELDS = ("auctions", "quantity", "min", "p10", "p25", "p50", "max")
METRICS = ("min", "p10", "p25", "p50")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id        TEXT PRIMARY KEY,      -- realm|faction|server epoch
    ts        INTEGER NOT NULL,      -- server epoch seconds (UTC)
    tz_offset INTEGER,               -- player's local UTC offset when recorded
    realm     TEXT NOT NULL,
    faction   TEXT,
    auctions  INTEGER,
    no_buyout INTEGER,
    items     INTEGER,
    source    TEXT
);
CREATE TABLE IF NOT EXISTS items (
    key     TEXT PRIMARY KEY,        -- "itemID" or "itemID:<suffix>", e.g. "15216:of the Bear"
    item_id INTEGER NOT NULL,
    suffix  TEXT,                    -- random suffix ("of the Bear"), NULL for plain items
    name    TEXT,
    quality INTEGER
);
CREATE TABLE IF NOT EXISTS prices (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    key         TEXT NOT NULL,
    auctions    INTEGER NOT NULL,
    quantity    INTEGER NOT NULL,
    min         INTEGER NOT NULL,    -- copper per unit
    p10         INTEGER NOT NULL,    -- quantity-weighted percentiles of unit buyout
    p25         INTEGER NOT NULL,
    p50         INTEGER NOT NULL,
    max         INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, key)
);
CREATE INDEX IF NOT EXISTS prices_by_key ON prices(key);
CREATE INDEX IF NOT EXISTS snapshots_by_ts ON snapshots(ts);
"""

DEFAULT_DB = Path(__file__).resolve().parent.parent / "ahsnapshot.sqlite"

_WOW_ROOTS = [
    r"C:\Program Files (x86)\World of Warcraft",
    r"C:\Program Files\World of Warcraft",
    r"D:\World of Warcraft",
    r"D:\Games\World of Warcraft",
    "/Applications/World of Warcraft",
]


def find_saved_variables() -> list[Path]:
    """Locate AHSnapshot.lua for every account and game flavor (_forever_, _retail_, ...)."""
    roots = list(_WOW_ROOTS)
    if os.environ.get("WOW_DIR"):
        roots.insert(0, os.environ["WOW_DIR"])
    found: list[Path] = []
    for root in roots:
        pattern = os.path.join(root, "*", "WTF", "Account", "*", "SavedVariables", SAVED_VAR)
        found.extend(Path(p) for p in glob.glob(pattern))
    return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@dataclass
class ImportResult:
    source: Path
    snapshots_in_file: int
    imported: int
    skipped: int
    outdated: int   # recorded by an addon version with the old (merged) item keys
    prices: int


# Item-key scheme the addon writes into each snapshot ("kv"). v1 snapshots merged every
# random suffix of an item under its itemID, so they're skipped.
MIN_KEY_VERSION = 2


def _parse_key(key: str) -> tuple[int, str | None]:
    item_id, _, suffix = key.partition(":")
    return int(item_id), (suffix or None)


def import_file(conn: sqlite3.Connection, path: Path | str) -> ImportResult:
    path = Path(path)
    data = luatable.load(path).get(SAVED_VAR_GLOBAL)
    if not isinstance(data, dict):
        raise ValueError(f"{path} has no {SAVED_VAR_GLOBAL} table")

    snapshots = data.get("snapshots") or []
    if isinstance(snapshots, dict):  # sparse table after trimming edge cases
        snapshots = [snapshots[k] for k in sorted(snapshots)]
    # v1 files cached names under merged keys, so don't trust them
    items = (data.get("items") or {}) if (data.get("version") or 1) >= MIN_KEY_VERSION else {}

    with conn:
        for key, info in items.items():
            key = str(key)
            try:
                item_id, suffix = _parse_key(key)
            except ValueError:
                continue
            info = info if isinstance(info, dict) else {}
            conn.execute(
                """INSERT INTO items(key, item_id, suffix, name, quality) VALUES (?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     name = COALESCE(excluded.name, items.name),
                     quality = COALESCE(excluded.quality, items.quality)""",
                (key, item_id, suffix, info.get("n"), info.get("q")),
            )

        imported = skipped = outdated = price_rows = 0
        for snap in snapshots:
            if (snap.get("kv") or 1) < MIN_KEY_VERSION:
                outdated += 1
                continue
            ts = int(snap["t"])
            realm = snap.get("realm") or "?"
            faction = snap.get("faction") or ""
            snap_id = f"{realm}|{faction}|{ts}"
            if conn.execute("SELECT 1 FROM snapshots WHERE id = ?", (snap_id,)).fetchone():
                skipped += 1
                continue
            entries = snap.get("items") or {}
            rows = []
            for key, encoded in entries.items():
                parts = str(encoded).split(",")
                if len(parts) != len(PRICE_FIELDS):
                    continue
                rows.append((snap_id, str(key), *map(int, parts)))
                key = str(key)
                try:
                    item_id, suffix = _parse_key(key)
                except ValueError:
                    continue
                conn.execute("INSERT OR IGNORE INTO items(key, item_id, suffix) VALUES (?,?,?)",
                             (key, item_id, suffix))
            if (snap.get("kv") or 1) == 2:
                # kv 2 (addon 0.2.0) put the first auctions of each suffix item under the plain
                # itemID before the base name had loaded: a mix of suffixes. Drop those rows.
                suffixed = {k.split(":", 1)[0] for k in map(str, entries) if ":" in k}
                rows = [r for r in rows if ":" in r[1] or r[1] not in suffixed]
            conn.execute(
                "INSERT INTO snapshots(id, ts, tz_offset, realm, faction, auctions, no_buyout, items, source)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (snap_id, ts, snap.get("tz"), realm, faction, snap.get("auctions"),
                 snap.get("noBuyout"), len(rows), str(path)),
            )
            conn.executemany(
                "INSERT INTO prices(snapshot_id, key, auctions, quantity, min, p10, p25, p50, max)"
                " VALUES (?,?,?,?,?,?,?,?,?)", rows)
            imported += 1
            price_rows += len(rows)

    return ImportResult(path, len(snapshots), imported, skipped, outdated, price_rows)
