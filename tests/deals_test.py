"""Deal finder test: hand-built price history with known deals, price drops and junk.

Run: python tests/deals_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "analyzer"))
from ahsnap import deals, store  # noqa: E402

G = 10000  # copper per gold
REALM, FACTION = "Test Realm", "Alliance"
T0 = 1_790_000_000
HOUR = 3600

failures = 0


def check(cond: bool, label: str) -> None:
    global failures
    print(f"  [{'ok' if cond else 'FAIL'}] {label}")
    failures += not cond


def build(conn, history: dict[str, list[tuple | None]], names: dict[str, str]) -> None:
    """history: key -> per snapshot (quantity, min, p10, p25, p50) or None if not listed."""
    n = max(len(v) for v in history.values())
    for i in range(n):
        sid = f"{REALM}|{FACTION}|{T0 + i * HOUR}"
        conn.execute("INSERT INTO snapshots(id, ts, tz_offset, realm, faction, auctions, no_buyout, items, source)"
                     " VALUES (?, ?, 0, ?, ?, 0, 0, 0, 'test')", (sid, T0 + i * HOUR, REALM, FACTION))
        for key, rows in history.items():
            r = rows[i] if i < len(rows) else None
            if r:
                q, mn, p10, p25, p50 = r
                conn.execute("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (sid, key, q, q, mn, p10, p25, p50, p50 * 2))
    for key, name in names.items():
        conn.execute("INSERT INTO items(key, item_id, name) VALUES (?, ?, ?)", (key, int(key.split(":")[0]), name))
    conn.commit()


def steady(q, price, n=8):
    return [(q, price, price, price, price)] * n


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = store.connect(Path(tmp) / "t.sqlite")
        history = {
            # 1. real deal: 20 listed at ~100g; now 25-50% of them at 40g, the rest still 100g
            "1": steady(20, 100 * G) + [(20, 40 * G, 40 * G, 40 * G, 100 * G)],
            # 2. single item, usually 50g, one listed at 10g
            "2": steady(1, 50 * G) + [(1, 10 * G, 10 * G, 10 * G, 10 * G)],
            # 3. market drop: everything listed is now cheap (more than half the units)
            "3": steady(30, 20 * G) + [(30, 8 * G, 8 * G, 8 * G, 8 * G)],
            # 4. jumpy item (troll listings): usual price meaningless
            "4": [(1, p, p, p, p) for p in (5 * G, 900 * G, 4 * G, 1000 * G, 6 * G, 800 * G, 5 * G, 950 * G)]
                 + [(1, 1 * G, 1 * G, 1 * G, 1 * G)],
            # 5. stale: already cheap in the previous snapshot
            "5": steady(1, 60 * G, 7) + [(1, 20 * G, 20 * G, 20 * G, 20 * G)] * 2,
            # 6. too little history
            "6": [None] * 5 + steady(1, 70 * G, 3) + [(1, 5 * G, 5 * G, 5 * G, 5 * G)],
            # 7. deal, but the next listing up is 85g, under the usual 100g: resale capped there
            "7": steady(10, 100 * G) + [(10, 60 * G, 60 * G, 85 * G, 85 * G)],
            # 8. tiny profit (under 1g)
            "8": steady(1, 1 * G) + [(1, 2000, 2000, 2000, 2000)],
        }
        names = {k: f"Item {k}" for k in history}
        build(conn, history, names)

        cfg = deals.Settings()
        found = {d.key: d for d in deals.find_deals(conn, cfg)}
        print("Deals:", sorted(found))
        check("1" in found, "partial undercut on a stacked item is flagged")
        d = found.get("1")
        if d:
            check(d.units == 5 and d.buy_at == 40 * G, f"at least 5 units (25%) at <= 40g (got {d.units} at {d.buy_at})")
            check(abs(d.profit - 5 * (95 * G - 40 * G)) < 1, "profit lower bound = 5 x (100g - 5% - 40g)")
        check("2" in found and abs(found["2"].profit - (47.5 * G - 10 * G)) < 1, "single cheap item flagged")
        check("3" not in found, "whole-market price drop is not a deal")
        check("4" not in found, "jumpy item skipped")
        check("5" not in found, "stale cheap listing hidden by default")
        check("6" not in found, "item with too little history skipped")
        d7 = found.get("7")
        check(d7 is not None and d7.resale == 85 * G, "resale capped at the next listing up (85g, not 100g)")
        check("8" not in found, "profit under 1g skipped")

        found_all = {d.key: d for d in deals.find_deals(conn, deals.Settings(include_stale=True))}
        check("5" in found_all and found_all["5"].cheap_for == 1, "--all shows the stale listing, cheap for 1")

        # backtest: add a follow-up snapshot where deal 1 was bought and deal 2 is still there
        n = len(history["1"])
        sid = f"{REALM}|{FACTION}|{T0 + n * HOUR}"
        conn.execute("INSERT INTO snapshots(id, ts, tz_offset, realm, faction, auctions, no_buyout, items, source)"
                     " VALUES (?, ?, 0, ?, ?, 0, 0, 0, 'test')", (sid, T0 + n * HOUR, REALM, FACTION))
        for key, row in {"1": (14, 100 * G, 100 * G, 100 * G, 100 * G), "2": (1, 10 * G, 10 * G, 10 * G, 10 * G),
                         "7": (10, 100 * G, 100 * G, 100 * G, 100 * G)}.items():
            q, mn, p10, p25, p50 = row
            conn.execute("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (sid, key, q, q, mn, p10, p25, p50, p50 * 2))
        conn.commit()
        bt = deals.backtest(conn, cfg)
        print(f"Backtest: {bt}")
        check(bt.deals >= 3, "backtest replays the deal snapshot")
        check(bt.gone >= 2, "deals 1 and 7 counted as gone")
        check(bt.still >= 1, "deal 2 counted as still there")
        conn.close()

    print("\nAll checks passed." if not failures else f"\n{failures} check(s) FAILED.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
