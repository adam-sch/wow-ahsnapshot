"""Analyses over imported snapshots."""

from __future__ import annotations

import math
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


def mann_whitney_p(a: list[float], b: list[float]) -> float:
    """Two-sided Mann-Whitney U test p-value (normal approximation with tie correction)."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return 1.0
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    n = n1 + n2
    ranks = [0.0] * n
    tie_term = 0.0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        t = j - i + 1
        tie_term += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, combined) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    var = n1 * n2 / 12 * ((n + 1) - tie_term / (n * (n - 1)))
    if var <= 0:
        return 1.0
    z = max(0.0, abs(u1 - mu) - 0.5) / math.sqrt(var)
    return math.erfc(z / math.sqrt(2))


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


# --------------------------------------------------------------------------- day vs night

@dataclass
class DayNightRow:
    realm: str
    faction: str
    key: str
    name: str
    n_day: int
    n_night: int
    days_day: int
    days_night: int
    med_day: float
    med_night: float
    qty_day: float
    qty_night: float
    diff_pct: float          # (night - day) / day * 100
    p_value: float


def day_vs_night(
    conn: sqlite3.Connection,
    *,
    metric: str = "p10",
    day_hours: set[int],
    night_hours: set[int],
    tz: tzinfo | None = None,
    min_obs: int = 3,
    min_days: int = 2,
    min_price: int = 0,
    realm: str | None = None,
    faction: str | None = None,
    since: int | None = None,
) -> list[DayNightRow]:
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    where, params = _filters(realm, faction, since)
    sql = f"""
        SELECT s.realm, s.faction, s.ts, p.key, p.{metric} AS price, p.quantity, i.name
        FROM prices p
        JOIN snapshots s ON s.id = p.snapshot_id
        LEFT JOIN items i ON i.key = p.key
        WHERE 1=1 {where}
    """
    groups: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {"name": None, "day": [], "night": [], "qd": [], "qn": [], "dd": set(), "dn": set()})
    hour_cache: dict[int, datetime] = {}
    for r in conn.execute(sql, params):
        ts = r["ts"]
        dt = hour_cache.get(ts)
        if dt is None:
            dt = hour_cache[ts] = local_dt(ts, tz)
        g = groups[(r["realm"], r["faction"], r["key"])]
        g["name"] = r["name"]
        if dt.hour in night_hours:
            g["night"].append(r["price"])
            g["qn"].append(r["quantity"])
            # a night that starts at 22:00 and ends at 06:00 counts as one session
            g["dn"].add((dt.toordinal() - (1 if dt.hour < 12 else 0)))
        elif dt.hour in day_hours:
            g["day"].append(r["price"])
            g["qd"].append(r["quantity"])
            g["dd"].add(dt.toordinal())

    out: list[DayNightRow] = []
    for (realm_, faction_, key), g in groups.items():
        if len(g["day"]) < min_obs or len(g["night"]) < min_obs:
            continue
        if len(g["dd"]) < min_days or len(g["dn"]) < min_days:
            continue
        med_day = statistics.median(g["day"])
        med_night = statistics.median(g["night"])
        if max(med_day, med_night) < min_price or med_day <= 0:
            continue
        out.append(DayNightRow(
            realm=realm_, faction=faction_, key=key, name=g["name"] or f"item {key}",
            n_day=len(g["day"]), n_night=len(g["night"]),
            days_day=len(g["dd"]), days_night=len(g["dn"]),
            med_day=med_day, med_night=med_night,
            qty_day=statistics.median(g["qd"]), qty_night=statistics.median(g["qn"]),
            diff_pct=(med_night - med_day) / med_day * 100,
            p_value=mann_whitney_p(g["day"], g["night"]),
        ))
    return out


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
