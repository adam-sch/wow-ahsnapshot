"""Day vs night, measured night by night against the days on either side.

Simply pooling all night snapshots and all day snapshots is misleading when prices drift:
on this server they fall about 2% a day, so a night always looks cheaper than the morning
before it and pricier than the morning after. Instead, each night is compared with the
average of the day before and the day after it ("sandwich"), which cancels a steady trend:

    effect(night D) = log(night D) - (log(day D) + log(day D+1)) / 2

An item's result is the median of its nightly effects, plus how many nights agree.
"""

from __future__ import annotations

import math
import random
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from .analysis import _filters, local_dt
from .store import METRICS

TIE = 0.02    # nightly changes within +/-2% count as "no difference"


@dataclass
class Settings:
    day_hours: set[int]
    night_hours: set[int]
    metric: str = "p10"
    min_nights: int = 3         # sandwiched nights the item must be measured on
    min_qty: float = 5          # median units listed, in each window of each night used
    min_price: int = 0          # copper; ignore items cheaper than this
    agree: float = 0.8          # share of measured nights that must point the same way
    min_effect: float = 0.05    # |median nightly change| needed to be listed
    tz: tzinfo | None = None


@dataclass
class ItemResult:
    realm: str
    faction: str
    key: str
    name: str
    nights: int
    effect: float               # median nightly price change, e.g. -0.12 = 12% cheaper at night
    qty_effect: float           # same for units listed
    lower: int                  # nights more than 2% cheaper
    higher: int                 # nights more than 2% pricier
    p_value: float              # two-sided sign test over lower vs higher
    day_price: float
    night_price: float
    day_qty: float
    night_qty: float
    nightly: list[float] = field(default_factory=list)

    @property
    def agreeing(self) -> int:
        return max(self.lower, self.higher)


@dataclass
class Report:
    nights: list[str]                       # sandwiched nights available, e.g. "Mon 11-16"
    items: list[ItemResult]                 # every item measured on >= min_nights
    market_price: float | None = None       # median nightly price change over all measured items
    market_qty: float | None = None
    share_lower: float = 0.0                # item-nights > 2% cheaper at night
    share_higher: float = 0.0
    trend_per_day: float | None = None      # median day-to-next-day price change
    chance_listed: float | None = None      # items expected to pass the filters by chance alone

    def listed(self, cfg: Settings) -> list[ItemResult]:
        return [r for r in self.items if _passes(r.nightly, cfg)]


def sign_test_p(lower: int, higher: int) -> float:
    n = lower + higher
    if n == 0:
        return 1.0
    k = min(lower, higher)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _passes(nightly: list[float], cfg: Settings) -> bool:
    if len(nightly) < cfg.min_nights:
        return False
    med = statistics.median(nightly)
    if abs(med) < cfg.min_effect:
        return False
    same = sum(1 for e in nightly if (e < -TIE if med < 0 else e > TIE))
    return same >= cfg.agree * len(nightly)


def _session(dt: datetime, cfg: Settings):
    """(date the session belongs to, 'day'|'night') or None. A night belongs to the evening it starts on."""
    if dt.hour in cfg.night_hours:
        start = dt.date() - timedelta(days=1) if dt.hour < 12 else dt.date()
        return start, "night"
    if dt.hour in cfg.day_hours:
        return dt.date(), "day"
    return None


def analyze(conn: sqlite3.Connection, cfg: Settings, *, realm=None, faction=None, since=None) -> Report:
    if cfg.metric not in METRICS:
        raise ValueError(f"metric must be one of {METRICS}")
    where, params = _filters(realm, faction, since)
    sessions: dict[str, tuple] = {}
    for s in conn.execute(f"SELECT id, ts FROM snapshots s WHERE 1=1 {where}", params):
        sess = _session(local_dt(s["ts"], cfg.tz), cfg)
        if sess:
            sessions[s["id"]] = sess
    nights = sorted({d for d, w in sessions.values() if w == "night"})
    days = {d for d, w in sessions.values() if w == "day"}
    sandwiched = [d for d in nights if d in days and d + timedelta(days=1) in days]
    labels = [d.strftime("%a %m-%d") for d in sandwiched]

    # (realm, faction, key) -> (date, window) -> [(price, qty)]
    obs: dict[tuple, dict[tuple, list]] = defaultdict(lambda: defaultdict(list))
    names: dict[str, str] = {}
    sql = f"""SELECT s.realm, s.faction, p.snapshot_id, p.key, p.{cfg.metric} AS price, p.quantity, i.name
              FROM prices p JOIN snapshots s ON s.id = p.snapshot_id LEFT JOIN items i ON i.key = p.key
              WHERE 1=1 {where}"""
    for r in conn.execute(sql, params):
        sess = sessions.get(r["snapshot_id"])
        if sess and r["price"] > 0:
            obs[(r["realm"], r["faction"] or "", r["key"])][sess].append((r["price"], r["quantity"]))
            if r["name"]:
                names[r["key"]] = r["name"]

    def window(by, d, w):
        v = by.get((d, w))
        if not v:
            return None
        return statistics.median(p for p, _ in v), statistics.median(q for _, q in v)

    items: list[ItemResult] = []
    all_price, all_qty, trend = [], [], []
    for (realm_, faction_, key), by in obs.items():
        nightly, nightly_q = [], []
        dp, nq_, dq, np_ = [], [], [], []
        for d in sandwiched:
            before, night, after = window(by, d, "day"), window(by, d, "night"), window(by, d + timedelta(days=1), "day")
            if not (before and night and after):
                continue
            if min(before[1], night[1], after[1]) < cfg.min_qty:
                continue
            day_log = (math.log(before[0]) + math.log(after[0])) / 2
            nightly.append(math.log(night[0]) - day_log)
            nightly_q.append(math.log(night[1]) - (math.log(before[1]) + math.log(after[1])) / 2)
            dp.append(math.exp(day_log))
            np_.append(night[0])
            dq.append((before[1] + after[1]) / 2)
            nq_.append(night[1])
        for d in sorted(days):
            a, b = window(by, d, "day"), window(by, d + timedelta(days=1), "day")
            if a and b and min(a[1], b[1]) >= cfg.min_qty:
                trend.append(math.log(b[0] / a[0]))
        if len(nightly) < cfg.min_nights:
            continue
        if max(statistics.median(dp), statistics.median(np_)) < cfg.min_price:
            continue
        nightly = [math.expm1(e) for e in nightly]        # as fractions: -0.12 = 12% cheaper
        all_price += nightly
        all_qty += [math.expm1(e) for e in nightly_q]
        lower = sum(e < -TIE for e in nightly)
        higher = sum(e > TIE for e in nightly)
        items.append(ItemResult(
            realm=realm_, faction=faction_, key=key, name=names.get(key) or f"item {key}",
            nights=len(nightly), effect=statistics.median(nightly),
            qty_effect=math.expm1(statistics.median(nightly_q)), lower=lower, higher=higher,
            p_value=sign_test_p(lower, higher), day_price=statistics.median(dp),
            night_price=statistics.median(np_), day_qty=statistics.median(dq), night_qty=statistics.median(nq_),
            nightly=nightly))

    rep = Report(labels, items)
    if all_price:
        rep.market_price = statistics.median(all_price)
        rep.market_qty = statistics.median(all_qty)
        rep.share_lower = sum(e < -TIE for e in all_price) / len(all_price)
        rep.share_higher = sum(e > TIE for e in all_price) / len(all_price)
    if trend:
        rep.trend_per_day = math.expm1(statistics.median(trend))
    if items:
        rep.chance_listed = _chance(items, cfg)
    return rep


def _chance(items: list[ItemResult], cfg: Settings, rounds: int = 50) -> float:
    """Items expected to pass the filters if day and night were randomly swapped each night.

    Each item's nightly changes are first centered on the market-wide night effect, so this
    asks: how many items would look this different from the market by luck?
    """
    rng = random.Random(1)
    market = statistics.median(e for r in items for e in r.nightly)
    total = 0
    for _ in range(rounds):
        for r in items:
            flipped = [market + (e - market) * rng.choice((1, -1)) for e in r.nightly]
            total += _passes(flipped, cfg)
    return total / rounds
