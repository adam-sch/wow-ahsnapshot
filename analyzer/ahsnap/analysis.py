"""Analyses over imported snapshots."""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, tzinfo

from .store import METRICS


# --------------------------------------------------------------------------- helpers

def parse_hours(spec: str) -> set[int]:
    """'22-6' -> {22, 23, 0, ..., 5}; '9-17' -> {9..16}; '0,1,2' -> {0,1,2}. End hour is exclusive."""
    hours: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = (int(x) % 24 for x in part.split("-", 1))
            h = start
            while True:
                hours.add(h)
                h = (h + 1) % 24
                if h == end:
                    break
        else:
            hours.add(int(part) % 24)
    if not hours:
        raise ValueError(f"no hours in {spec!r}")
    return hours


def local_dt(ts: int, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def format_money(copper: float | int | None) -> str:
    if copper is None:
        return "-"
    copper = int(round(copper))
    g, rem = divmod(copper, 10000)
    s, c = divmod(rem, 100)
    if g:
        return f"{g:,}g {s:02d}s"
    if s:
        return f"{s}s {c:02d}c"
    return f"{c}c"


def _filters(realm: str | None, faction: str | None, since: int | None):
    clauses, params = [], []
    if realm:
        clauses.append("s.realm = ? COLLATE NOCASE")
        params.append(realm)
    if faction:
        clauses.append("s.faction = ? COLLATE NOCASE")
        params.append(faction)
    if since:
        clauses.append("s.ts >= ?")
        params.append(since)
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


# --------------------------------------------------------------------------- lookups

def find_items(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    query = query.strip()
    if query.replace(":", "").isdigit():
        rows = conn.execute("SELECT * FROM items WHERE key = ? OR item_id = ? ORDER BY key",
                            (query, int(query.split(":")[0]))).fetchall()
        if rows:
            return rows
    exact = conn.execute("SELECT * FROM items WHERE name = ? COLLATE NOCASE ORDER BY key", (query,)).fetchall()
    if exact:
        return exact
    return conn.execute("SELECT * FROM items WHERE name LIKE ? ORDER BY name LIMIT ?",
                        (f"%{query}%", limit)).fetchall()


def item_label(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT name FROM items WHERE key = ?", (key,)).fetchone()
    return row["name"] if row and row["name"] else f"item {key}"


# --------------------------------------------------------------------------- per item

@dataclass
class HourRow:
    hour: int
    n: int
    med_price: float | None
    lo_price: float | None   # lower quartile of the metric across snapshots in that hour
    hi_price: float | None
    med_qty: float | None


def hourly_profile(conn, key: str, *, metric: str = "p10", tz=None, realm=None, faction=None,
                   since=None) -> list[HourRow]:
    where, params = _filters(realm, faction, since)
    rows = conn.execute(
        f"SELECT s.ts, p.{metric} AS price, p.quantity FROM prices p JOIN snapshots s ON s.id = p.snapshot_id"
        f" WHERE p.key = ? {where}", [key, *params]).fetchall()
    by_hour: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for r in rows:
        by_hour[local_dt(r["ts"], tz).hour].append((r["price"], r["quantity"]))
    out = []
    for h in range(24):
        vals = by_hour.get(h, [])
        if not vals:
            out.append(HourRow(h, 0, None, None, None, None))
            continue
        prices = sorted(v[0] for v in vals)
        q = statistics.quantiles(prices, n=4, method="inclusive") if len(prices) >= 2 else [prices[0]] * 3
        out.append(HourRow(h, len(vals), statistics.median(prices), q[0], q[2],
                           statistics.median(v[1] for v in vals)))
    return out


def history(conn, key: str, *, realm=None, faction=None, since=None) -> list[sqlite3.Row]:
    where, params = _filters(realm, faction, since)
    return conn.execute(
        f"""SELECT s.ts, s.realm, s.faction, p.* FROM prices p JOIN snapshots s ON s.id = p.snapshot_id
            WHERE p.key = ? {where} ORDER BY s.ts""", [key, *params]).fetchall()
