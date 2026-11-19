"""Deal finder: listings in a snapshot that are well below the item's usual price.

A snapshot only stores a summary per item (cheapest price, quantity-weighted 10th/25th/50th
percentile, total quantity), not every listing. So the finder works with lower bounds:
if the 25th percentile is under the buy-below price, at least 25% of the units listed
cost no more than that, and so on.

"Usual price" is the median of the item's 10th-percentile price over earlier snapshots:
roughly what the cheapest listings normally cost, i.e. what you'd have to match to resell.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass

AH_CUT = 0.05     # the auction house keeps 5% of the sale price
FRACTIONS = (("min", None), ("p10", 0.10), ("p25", 0.25), ("p50", 0.50))


@dataclass
class Deal:
    realm: str
    faction: str
    key: str
    name: str
    ts: int                 # snapshot time
    price: int              # cheapest unit now
    usual: float            # median p10 over earlier snapshots
    buy_below: float        # usual * (1 - discount)
    units: int              # at least this many units cost <= buy_at
    buy_at: int             # ... and this is the most any of those cost
    resale: float           # price to relist at: the usual price, or the next listing up if that's lower
    profit_unit: float      # resale minus the AH cut, minus what the cheapest unit costs
    profit: float           # lower bound for buying `units` and reselling at `resale`
    quantity: int           # units listed now
    usual_qty: float        # median units listed in earlier snapshots
    seen: int               # earlier snapshots the item appeared in
    history: int            # earlier snapshots in the window
    spread: float           # (p75 - p25) / median of earlier p10s: how jumpy the usual price is
    dip_rate: float         # share of earlier snapshots with a price this low or lower
    cheap_for: int          # consecutive earlier snapshots already under buy_below (0 = new)

    @property
    def discount_pct(self) -> float:
        return (1 - self.price / self.usual) * 100


@dataclass
class Settings:
    discount: float = 0.30          # flag listings at least this far under the usual price
    min_seen: int = 6               # earlier snapshots the item must appear in
    min_presence: float = 0.25      # ... and as a share of the history window
    min_profit: int = 10000         # copper; estimated total profit must reach this
    min_usual: int = 0              # copper; ignore items usually cheaper than this
    max_spread: float | None = 0.5  # skip items whose usual price is this jumpy
    include_stale: bool = False     # also show listings that were already this cheap last snapshot
    history_seconds: int = 14 * 86400


def _load(conn: sqlite3.Connection, realm: str | None, faction: str | None, since: int | None):
    """All snapshots (oldest first) and price rows, grouped by (realm, faction)."""
    clauses, params = [], []
    if realm:
        clauses.append("realm = ? COLLATE NOCASE")
        params.append(realm)
    if faction:
        clauses.append("faction = ? COLLATE NOCASE")
        params.append(faction)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    snaps = conn.execute(f"SELECT id, ts, realm, faction FROM snapshots{where} ORDER BY ts", params).fetchall()
    markets: dict[tuple[str, str], list] = defaultdict(list)
    for s in snaps:
        markets[(s["realm"], s["faction"] or "")].append(s)
    return markets


def _rows(conn: sqlite3.Connection, snapshot_ids: list[str]) -> dict[str, dict[str, tuple]]:
    """snapshot id -> key -> (quantity, min, p10, p25, p50)."""
    out: dict[str, dict[str, tuple]] = {}
    for sid in snapshot_ids:
        out[sid] = {r[0]: r[1:] for r in conn.execute(
            "SELECT key, quantity, min, p10, p25, p50 FROM prices WHERE snapshot_id = ?", (sid,))}
    return out


def deals_at(index: int, snaps: list, rows: dict[str, dict[str, tuple]], names: dict[str, str],
             cfg: Settings) -> list[Deal]:
    """Deals in snaps[index], judged against the snapshots before it in the history window."""
    target = snaps[index]
    earliest = target["ts"] - cfg.history_seconds
    earlier = [s for s in snaps[:index] if s["ts"] >= earliest]
    if not earlier:
        return []
    out: list[Deal] = []
    for key, (qty, pmin, p10, p25, p50) in rows[target["id"]].items():
        past = [rows[s["id"]].get(key) for s in earlier]    # oldest first, None where not listed
        seen = [r for r in past if r]
        if len(seen) < cfg.min_seen or len(seen) < cfg.min_presence * len(earlier):
            continue
        p10s = sorted(r[2] for r in seen)
        usual = statistics.median(p10s)
        if usual <= 0 or usual < cfg.min_usual:
            continue
        buy_below = usual * (1 - cfg.discount)
        if pmin > buy_below:
            continue
        q1, _, q3 = statistics.quantiles(p10s, n=4, method="inclusive") if len(p10s) > 1 else (usual,) * 3
        spread = (q3 - q1) / usual
        if cfg.max_spread is not None and spread > cfg.max_spread:
            continue

        cheap_for = 0
        for r in reversed(past):
            if not r or r[1] > buy_below:
                break
            cheap_for += 1
        if cheap_for and not cfg.include_stale:
            continue

        # After buying the cheap units you compete with the next listing up. Its price is at most
        # the first current percentile above buy_below. If every percentile is that cheap and there
        # are more than a couple of units, the whole market has moved down: not a deal.
        above = [p for p in (p10, p25, p50) if p > buy_below]
        if above:
            relist = min(usual, above[0])
        elif qty <= 2:
            relist = usual
        else:
            continue
        resale = relist * (1 - AH_CUT)

        # best provable lower bound on profit: "at least N units cost at most P"
        best = (0.0, 1, pmin)
        for (_, frac), price in zip(FRACTIONS, (pmin, p10, p25, p50)):
            if price > buy_below:
                break
            n = 1 if frac is None else max(1, math.ceil(frac * qty))
            gain = n * (resale - price)
            if gain > best[0]:
                best = (gain, n, price)
        profit, units, buy_at = best
        if profit < cfg.min_profit:
            continue
        out.append(Deal(
            realm=target["realm"], faction=target["faction"] or "", key=key,
            name=names.get(key) or f"item {key}", ts=target["ts"],
            price=pmin, usual=usual, buy_below=buy_below, units=units, buy_at=buy_at,
            resale=relist, profit_unit=resale - pmin, profit=profit, quantity=qty,
            usual_qty=statistics.median(r[0] for r in seen), seen=len(seen), history=len(earlier),
            spread=spread, dip_rate=sum(r[1] <= pmin for r in seen) / len(seen), cheap_for=cheap_for,
        ))
    out.sort(key=lambda d: -d.profit)
    return out


def _names(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT key, name FROM items WHERE name IS NOT NULL")}


def find_deals(conn: sqlite3.Connection, cfg: Settings, *, realm=None, faction=None) -> list[Deal]:
    """Deals in the newest snapshot of each realm/faction."""
    names = _names(conn)
    out: list[Deal] = []
    for snaps in _load(conn, realm, faction, None).values():
        earliest = snaps[-1]["ts"] - cfg.history_seconds
        snaps = [s for s in snaps if s["ts"] >= earliest]
        rows = _rows(conn, [s["id"] for s in snaps])
        out += deals_at(len(snaps) - 1, snaps, rows, names, cfg)
    out.sort(key=lambda d: -d.profit)
    return out


@dataclass
class Backtest:
    snapshots: int = 0          # snapshots that had a following snapshot soon enough to check
    deals: int = 0
    gone: int = 0               # cheapest price was back above buy-below in the next snapshot
    partly: int = 0             # still cheap, but fewer units listed
    still: int = 0              # same or more units still listed that cheap
    delisted: int = 0           # item not listed at all in the next snapshot
    profit_gone: float = 0.0    # estimated profit of deals that were gone
    normal: int = 0             # comparison: cheapest listings at a normal price (within 10% of usual)
    normal_gone: int = 0        # ... whose price was gone by the next snapshot


def backtest(conn: sqlite3.Connection, cfg: Settings, *, max_gap: int = 3 * 3600,
             realm=None, faction=None, since=None) -> Backtest:
    """Replay every snapshot: were its deals gone in the next snapshot (bought, or cancelled)?"""
    names = _names(conn)
    bt = Backtest()
    for snaps in _load(conn, realm, faction, since).values():
        rows = _rows(conn, [s["id"] for s in snaps])
        for i in range(1, len(snaps) - 1):
            if snaps[i + 1]["ts"] - snaps[i]["ts"] > max_gap:
                continue
            bt.snapshots += 1
            nxt = rows[snaps[i + 1]["id"]]
            _baseline(bt, i, snaps, rows, nxt, cfg)
            for d in deals_at(i, snaps, rows, names, cfg):
                bt.deals += 1
                r = nxt.get(d.key)
                if r is None:
                    bt.delisted += 1
                    bt.gone += 1
                    bt.profit_gone += d.profit
                elif r[1] > d.buy_below:
                    bt.gone += 1
                    bt.profit_gone += d.profit
                elif r[0] < d.quantity:
                    bt.partly += 1
                else:
                    bt.still += 1
    return bt


def _baseline(bt: Backtest, index: int, snaps: list, rows: dict, nxt: dict, cfg: Settings) -> None:
    """How often an ordinary cheapest listing disappears between two snapshots."""
    earliest = snaps[index]["ts"] - cfg.history_seconds
    earlier = [rows[s["id"]] for s in snaps[:index] if s["ts"] >= earliest]
    for key, (_, pmin, *_rest) in rows[snaps[index]["id"]].items():
        seen = [r[key] for r in earlier if key in r]
        if len(seen) < cfg.min_seen or len(seen) < cfg.min_presence * len(earlier):
            continue
        usual = statistics.median(r[2] for r in seen)
        if not 0.9 * usual <= pmin <= 1.1 * usual:
            continue
        bt.normal += 1
        r = nxt.get(key)
        bt.normal_gone += r is None or r[1] > pmin
