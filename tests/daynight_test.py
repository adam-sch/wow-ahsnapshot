"""Day/night test: a falling market with known night effects.

Prices fall 3% a day for every item. On top of that, one item is 20% cheaper at night, one is
25% pricier, one has no night effect, and one has a night effect but is too thin to trust.
Pooling all day and all night snapshots would wrongly call the neutral item cheaper at night
(a night always comes after its day); comparing each night with the days on both sides must not.

Run: python tests/daynight_test.py
"""

from __future__ import annotations

import math
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))
from ahsnap import daynight, store  # noqa: E402
from ahsnap.analysis import parse_hours  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 11, 2, tzinfo=UTC)    # a Monday
DAYS = 6
TREND = 0.97                                  # 3% cheaper every day
# key -> (base price in copper, night price factor, quantity by day, night quantity factor)
ITEMS = {
    "1": (10000, 0.80, 20, 1.3),   # 20% cheaper at night, 30% more listed
    "2": (5000, 1.25, 20, 1.3),    # 25% pricier at night
    "3": (8000, 1.00, 20, 1.3),    # no night effect
    "4": (9000, 0.50, 1, 1.0),     # thin: 1 listed
}
TIMES = [timedelta(hours=h) for h in (10, 14, 23, 26)]   # 10:00, 14:00, 23:00, 02:00 next day

failures = 0


def check(cond: bool, label: str) -> None:
    global failures
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    failures += not cond


def build(conn, days: int) -> None:
    for day in range(days):
        for offset in TIMES:
            when = START + timedelta(days=day) + offset
            if when >= START + timedelta(days=days):
                continue
            ts = int(when.timestamp())
            sid = f"Realm|Alliance|{ts}"
            conn.execute("INSERT INTO snapshots(id, ts, tz_offset, realm, faction, auctions, no_buyout, items, source)"
                         " VALUES (?, ?, 0, 'Realm', 'Alliance', 0, 0, 0, 'test')", (sid, ts))
            night = when.hour >= 22 or when.hour < 6
            elapsed = (when - START).total_seconds() / 86400
            for key, (base, night_factor, qty, night_qty) in ITEMS.items():
                price = round(base * TREND ** elapsed * (night_factor if night else 1))
                q = round(qty * (night_qty if night else 1))
                conn.execute("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (sid, key, q, q, price, price, price, price, price))
    for key in ITEMS:
        conn.execute("INSERT INTO items(key, item_id, name) VALUES (?, ?, ?)", (key, int(key), f"Item {key}"))
    conn.commit()


def settings(**kw) -> daynight.Settings:
    return daynight.Settings(day_hours=parse_hours("9-18"), night_hours=parse_hours("22-6"), tz=UTC, **kw)


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = store.connect(Path(tmp) / "t.sqlite")
        build(conn, DAYS)
        cfg = settings()
        rep = daynight.analyze(conn, cfg)
        by = {r.key: r for r in rep.items}
        print("Nights:", rep.nights)
        check(len(rep.nights) == DAYS - 1, f"{DAYS - 1} nights have a day on both sides")
        check("1" in by and abs(by["1"].effect + 0.20) < 0.005, f"item 1: -20% at night (got {by['1'].effect:+.3f})")
        check("2" in by and abs(by["2"].effect - 0.25) < 0.005, f"item 2: +25% at night (got {by['2'].effect:+.3f})")
        check("3" in by and abs(by["3"].effect) < 0.005,
              f"item 3: no night effect despite the falling market (got {by['3'].effect:+.3f})")
        check("4" not in by, "thin item (1 listed) skipped")
        check(abs(by["1"].qty_effect - 0.30) < 0.01, f"30% more listed at night (got {by['1'].qty_effect:+.3f})")
        check(rep.trend_per_day is not None and abs(rep.trend_per_day + 0.03) < 0.002,
              f"market trend -3%/day (got {rep.trend_per_day:+.4f})")
        check(by["1"].lower == DAYS - 1 and by["1"].higher == 0, "item 1 cheaper on every night")
        listed = {r.key for r in rep.listed(cfg)}
        check(listed == {"1", "2"}, f"listed: items 1 and 2 only (got {sorted(listed)})")
        check(abs(daynight.sign_test_p(5, 0) - 0.0625) < 1e-9, "sign test p for 5/5 nights = 0.0625")

        # the old pooled comparison would have been fooled: show it still is, so the test means something
        nights = [r for r in conn.execute("SELECT s.ts, p.price FROM (SELECT snapshot_id, p10 AS price FROM prices "
                                          "WHERE key = '3') p JOIN snapshots s ON s.id = p.snapshot_id")]
        night_prices = [p for ts, p in nights if datetime.fromtimestamp(ts, UTC).hour in cfg.night_hours]
        day_prices = [p for ts, p in nights if datetime.fromtimestamp(ts, UTC).hour in cfg.day_hours]
        pooled = math.exp(sum(map(math.log, night_prices)) / len(night_prices)
                          - sum(map(math.log, day_prices)) / len(day_prices)) - 1
        check(pooled < -0.005,f"(pooled comparison would show item 3 at {pooled:+.1%}, the trap this avoids)")
        conn.close()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = store.connect(Path(tmp) / "t.sqlite")
        build(conn, 2)
        rep = daynight.analyze(conn, settings())
        check(len(rep.nights) == 1 and not rep.items, "with 2 days: 1 night measurable, no item reaches 3 nights")
        conn.close()

    print("\nAll checks passed." if not failures else f"\n{failures} check(s) FAILED.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
