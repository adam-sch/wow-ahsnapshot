"""Command-line interface: python ahs.py <command> ..."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import analysis, store
from .analysis import format_money


def _tz(name: str | None):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, missing tzdata on Windows
        sys.exit(f"Unknown time zone {name!r} ({exc}). On Windows you may need: pip install tzdata")


def _since(days: float | None) -> int | None:
    return int((datetime.now() - timedelta(days=days)).timestamp()) if days else None


def _table(headers: list[str], rows: list[list], align: str | None = None) -> None:
    cells = [[str(c) for c in row] for row in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]
    align = align or "l" + "r" * (len(headers) - 1)

    def fmt(row):
        return "  ".join(c.ljust(w) if a == "l" else c.rjust(w) for c, w, a in zip(row, widths, align))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print(fmt(r))


def _resolve_item(conn, query: str) -> str:
    matches = analysis.find_items(conn, query)
    if not matches:
        sys.exit(f"No item matching {query!r}. Run 'import' first, or check the spelling.")
    if len(matches) > 1 and not query.replace(":", "").isdigit():
        exact = [m for m in matches if (m["name"] or "").lower() == query.lower()]
        if len(exact) != 1:
            print(f"{len(matches)} items match {query!r}; showing the first. Use the key to pick one:")
            for m in matches[:15]:
                print(f"  {m['key']:>12}  {m['name']}")
            print()
        else:
            matches = exact
    return matches[0]["key"]


# --------------------------------------------------------------------------- commands

def cmd_import(args, conn):
    paths = [Path(p) for p in args.file] if args.file else store.find_saved_variables()
    if not paths:
        sys.exit("Could not find AHSnapshot.lua. Pass it with --file, or set WOW_DIR to your "
                 "World of Warcraft folder. (It appears after you log out or /reload once with the addon.)")
    for path in paths:
        r = store.import_file(conn, path)
        print(f"{path}\n  {r.snapshots_in_file} snapshots in file: {r.imported} new, "
              f"{r.skipped} already imported ({r.prices:,} price rows added)")
        if r.outdated:
            print(f"  {r.outdated} skipped: recorded by an older addon version that merged random-suffix "
                  "gear (\"of the Bear\") into one item")
    total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"Database now holds {total} snapshots: {args.db}")


def cmd_snapshots(args, conn):
    where, params = analysis._filters(args.realm, args.faction, _since(args.days))
    rows = conn.execute(f"SELECT * FROM snapshots s WHERE 1=1 {where} ORDER BY ts", params).fetchall()
    tz = _tz(args.tz)
    _table(["when (local)", "realm", "faction", "auctions", "items"],
           [[analysis.local_dt(r["ts"], tz).strftime("%a %Y-%m-%d %H:%M"), r["realm"], r["faction"],
             f"{r['auctions'] or 0:,}", f"{r['items']:,}"] for r in rows], "lllrr")
    if rows:
        hours = [0] * 24
        for r in rows:
            hours[analysis.local_dt(r["ts"], tz).hour] += 1
        print("\nSnapshots by hour of day (you need both day and night coverage for day/night analysis):")
        print("  " + " ".join(f"{h:>2}" for h in range(24)))
        print("  " + " ".join(f"{c:>2}" if c else " ." for c in hours))


def cmd_daynight(args, conn):
    tz = _tz(args.tz)
    night = analysis.parse_hours(args.night)
    day = analysis.parse_hours(args.day) if args.day else set(range(24)) - night
    rows = analysis.day_vs_night(
        conn, metric=args.metric, day_hours=day, night_hours=night, tz=tz,
        min_obs=args.min_obs, min_days=args.min_days, min_price=int(args.min_price * 10000),
        realm=args.realm, faction=args.faction, since=_since(args.days))
    if args.max_p is not None:
        rows = [r for r in rows if r.p_value <= args.max_p]

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["realm", "faction", "key", "name", "n_day", "n_night", "days_day", "days_night",
                        f"median_{args.metric}_day_copper", f"median_{args.metric}_night_copper",
                        "median_qty_day", "median_qty_night", "night_vs_day_pct", "p_value"])
            for r in sorted(rows, key=lambda r: r.diff_pct):
                w.writerow([r.realm, r.faction, r.key, r.name, r.n_day, r.n_night, r.days_day, r.days_night,
                            round(r.med_day), round(r.med_night), r.qty_day, r.qty_night,
                            round(r.diff_pct, 2), round(r.p_value, 5)])
        print(f"Wrote {len(rows)} items to {args.csv}")

    if not rows:
        print("No items have enough day AND night observations yet. Check coverage with 'snapshots', "
              "or lower --min-obs / --min-days.")
        return

    multi_realm = len({(r.realm, r.faction) for r in rows}) > 1
    print(f"Metric: {args.metric} unit buyout, median across snapshots. "
          f"Night = hours {sorted(night)}, day = hours {sorted(day)}.\n")

    def show(title, subset):
        if not subset:
            return
        print(title)
        headers = (["realm"] if multi_realm else []) + [
            "item", "day", "night", "night vs day", "qty day", "qty night", "obs d/n", "p"]
        _table(headers, [
            ([f"{r.realm}-{r.faction}"] if multi_realm else []) + [
                r.name[:40], format_money(r.med_day), format_money(r.med_night), f"{r.diff_pct:+.1f}%",
                f"{r.qty_day:g}", f"{r.qty_night:g}", f"{r.n_day}/{r.n_night}", f"{r.p_value:.3f}"]
            for r in subset], ("l" if multi_realm else "") + "lrrrrrrr")
        print()

    cheaper_night = sorted((r for r in rows if r.diff_pct < 0), key=lambda r: r.diff_pct)[:args.top]
    pricier_night = sorted((r for r in rows if r.diff_pct > 0), key=lambda r: -r.diff_pct)[:args.top]
    show(f"Cheaper at night (buy at night, {len(cheaper_night)} shown):", cheaper_night)
    show(f"More expensive at night (sell at night, {len(pricier_night)} shown):", pricier_night)
    print(f"{len(rows)} items compared. 'p' is a Mann-Whitney test; below ~0.05 is less likely to be noise, "
          "but consecutive snapshots aren't independent, so treat it as a guide.")


def cmd_hourly(args, conn):
    key = _resolve_item(conn, args.item)
    tz = _tz(args.tz)
    rows = analysis.hourly_profile(conn, key, metric=args.metric, tz=tz, realm=args.realm,
                                   faction=args.faction, since=_since(args.days))
    print(f"{analysis.item_label(conn, key)} [{key}] - {args.metric} unit buyout by local hour\n")
    prices = [r.med_price for r in rows if r.med_price is not None]
    if not prices:
        print("No data for this item.")
        return
    lo, hi = min(prices), max(prices)
    width = 30
    table = []
    for r in rows:
        if r.med_price is None:
            table.append([f"{r.hour:02d}:00", 0, "-", "-", "-", ""])
            continue
        bar = "#" * (1 + round((r.med_price - lo) / (hi - lo) * (width - 1))) if hi > lo else "#"
        table.append([f"{r.hour:02d}:00", r.n, format_money(r.med_price),
                      f"{format_money(r.lo_price)} - {format_money(r.hi_price)}", f"{r.med_qty:g}", bar])
    _table(["hour", "obs", "median", "middle 50%", "qty", ""], table, "lrrrrl")


def cmd_history(args, conn):
    key = _resolve_item(conn, args.item)
    tz = _tz(args.tz)
    rows = analysis.history(conn, key, realm=args.realm, faction=args.faction, since=_since(args.days))
    print(f"{analysis.item_label(conn, key)} [{key}]\n")
    _table(["when (local)", "auctions", "qty", "min", "p10", "p25", "p50"],
           [[analysis.local_dt(r["ts"], tz).strftime("%a %m-%d %H:%M"), r["auctions"], r["quantity"],
             format_money(r["min"]), format_money(r["p10"]), format_money(r["p25"]), format_money(r["p50"])]
            for r in rows], "lrrrrrr")


def cmd_export(args, conn):
    tz = _tz(args.tz)
    where, params = analysis._filters(args.realm, args.faction, _since(args.days))
    rows = conn.execute(f"""
        SELECT s.ts, s.realm, s.faction, p.key, i.item_id, i.suffix, i.name, i.quality,
               p.auctions, p.quantity, p.min, p.p10, p.p25, p.p50, p.max
        FROM prices p JOIN snapshots s ON s.id = p.snapshot_id LEFT JOIN items i ON i.key = p.key
        WHERE 1=1 {where} ORDER BY s.ts, p.key""", params)
    n = 0
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "local_time", "local_hour", "weekday", "realm", "faction", "key", "item_id",
                    "suffix", "name", "quality", "auctions", "quantity",
                    "min_copper", "p10_copper", "p25_copper", "p50_copper", "max_copper"])
        for r in rows:
            dt = analysis.local_dt(r["ts"], tz)
            w.writerow([r["ts"], dt.strftime("%Y-%m-%d %H:%M"), dt.hour, dt.strftime("%a"), r["realm"],
                        r["faction"], r["key"], r["item_id"], r["suffix"], r["name"], r["quality"],
                        r["auctions"], r["quantity"], r["min"], r["p10"], r["p25"], r["p50"], r["max"]])
            n += 1
    print(f"Wrote {n:,} rows to {args.out}")


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(store.DEFAULT_DB), help="SQLite database (default: %(default)s)")
    common.add_argument("--realm", help="only this realm")
    common.add_argument("--faction", help="only this faction (Alliance/Horde)")
    common.add_argument("--days", type=float, help="only snapshots from the last N days")
    common.add_argument("--tz", help="IANA time zone for hours of day, e.g. America/New_York "
                                     "(default: this computer's local time)")

    p = argparse.ArgumentParser(prog="ahs", description="Analyze AHSnapshot auction house data.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("import", parents=[common], help="import AHSnapshot.lua from the game into the database")
    s.add_argument("--file", action="append", help="path to AHSnapshot.lua (default: auto-detect)")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("snapshots", parents=[common], help="list imported snapshots and hour-of-day coverage")
    s.set_defaults(func=cmd_snapshots)

    s = sub.add_parser("daynight", parents=[common], help="items whose price differs between night and day")
    s.add_argument("--night", default="22-6", help="night hours, end exclusive (default: %(default)s)")
    s.add_argument("--day", help="day hours (default: every hour not in --night)")
    s.add_argument("--metric", choices=store.METRICS, default="p10",
                   help="price per snapshot: min, or quantity-weighted p10/p25/p50 (default: %(default)s)")
    s.add_argument("--min-obs", type=int, default=3, help="min snapshots in each window (default: %(default)s)")
    s.add_argument("--min-days", type=int, default=2,
                   help="min distinct days/nights observed (default: %(default)s)")
    s.add_argument("--min-price", type=float, default=0, help="ignore items cheaper than this many gold "
                                                                 "(default: %(default)s)")
    s.add_argument("--max-p", type=float, help="only show items with p-value at or below this")
    s.add_argument("--top", type=int, default=20, help="rows per list (default: %(default)s)")
    s.add_argument("--csv", help="also write all compared items to this CSV")
    s.set_defaults(func=cmd_daynight)

    s = sub.add_parser("hourly", parents=[common], help="one item's price profile by hour of day")
    s.add_argument("item", help="item name (or part of it), item ID, or key like 12345:678")
    s.add_argument("--metric", choices=store.METRICS, default="p10")
    s.set_defaults(func=cmd_hourly)

    s = sub.add_parser("history", parents=[common], help="one item's prices in every snapshot")
    s.add_argument("item")
    s.set_defaults(func=cmd_history)

    s = sub.add_parser("export", parents=[common], help="export everything to a CSV (for Excel/pandas)")
    s.add_argument("out", help="output CSV path")
    s.set_defaults(func=cmd_export)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    conn = store.connect(args.db)
    try:
        args.func(args, conn)
    finally:
        conn.close()
