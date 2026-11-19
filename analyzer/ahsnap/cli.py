"""Command-line interface: python ahs.py <command> ..."""

from __future__ import annotations

import argparse
import os
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import analysis, daynight, deals, store, sync
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

OUTDATED_NOTE = ("recorded by an older addon version that merged random-suffix gear "
                 "(\"of the Bear\") into one item")


def _game_files(args) -> list[Path]:
    return [Path(p) for p in args.file] if args.file else store.find_saved_variables()


def cmd_import(args, conn):
    folder = None if args.local_only else sync.sync_dir(args.from_dir)
    paths = [] if args.sync_only else _game_files(args)
    if not paths and not folder:
        sys.exit("Could not find AHSnapshot.lua. Pass it with --file, set WOW_DIR to your World of Warcraft "
                 "folder, or import from a shared folder (--from, or 'ahs.py config --sync-dir').")
    if folder:
        problem = sync.folder_problem(folder)
        if problem:
            sys.exit(f"Shared folder not reachable: {folder}\n  {problem}")
        r = sync.import_folder(conn, folder)
        print(f"{folder}\n  {r.files} snapshot files, {r.new_files} new: {r.imported} imported, "
              f"{r.skipped} already in the database ({r.prices:,} price rows added)")
        if r.outdated:
            print(f"  {r.outdated} skipped: {OUTDATED_NOTE}")
        for err in r.errors:
            print(f"  could not read {err} (will retry next import)")
    for path in paths:
        r = store.import_file(conn, path)
        print(f"{path}\n  {r.snapshots_in_file} snapshots in file: {r.imported} new, "
              f"{r.skipped} already imported ({r.prices:,} price rows added)")
        if r.outdated:
            print(f"  {r.outdated} skipped: {OUTDATED_NOTE}")
    total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"Database now holds {total} snapshots: {args.db}")


def cmd_push(args, conn):
    folder = sync.sync_dir(args.to)
    if not folder:
        sys.exit(r"No shared folder set. Run: python ahs.py config --sync-dir \\YOUR-NAS\share\AHSnapshot "
                 "(or pass --to)")
    problem = sync.folder_problem(folder)
    if problem:
        sys.exit(f"Shared folder not reachable: {folder}\n  {problem}")
    paths = _game_files(args)
    if not paths:
        sys.exit("Could not find AHSnapshot.lua on this PC. Log out or /reload once in game, or pass --file.")
    for path in paths:
        r = sync.push_file(path, folder)
        print(f"{path}\n  {r.written} new snapshots copied to {folder}, {r.already_there} were already there")
        if r.outdated:
            print(f"  {r.outdated} not copied: {OUTDATED_NOTE}")
    print("All usable snapshots from this PC are in the shared folder; in game, '/ahs clear yes' "
          "is safe whenever you want to shrink the game file.")


def cmd_config(args, conn):
    cfg = sync.load_config()
    if args.sync_dir is not None:
        if args.sync_dir:
            cfg["sync_dir"] = args.sync_dir
        else:
            cfg.pop("sync_dir", None)
        sync.save_config(cfg)
    folder = sync.sync_dir()
    print(f"Config file  : {sync.CONFIG_PATH}")
    print(f"Shared folder: {folder or '(not set)'}"
          + ("  [from AHS_SYNC_DIR]" if os.environ.get("AHS_SYNC_DIR") else ""))
    if folder:
        problem = sync.folder_problem(folder)
        print(f"  reachable  : {'yes' if not problem else 'NO - ' + problem}")


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


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:+.1f}%"


def _short_pct(x: float) -> str:
    return ">+999" if x > 9.99 else f"{x * 100:+.0f}"


def cmd_daynight(args, conn):
    night = analysis.parse_hours(args.night)
    day = analysis.parse_hours(args.day) - night
    cfg = daynight.Settings(
        day_hours=day, night_hours=night, metric=args.metric, min_nights=args.min_nights,
        min_qty=args.min_qty, min_price=int(args.min_price * 10000), agree=args.agree / 100,
        min_effect=args.min_effect / 100, tz=_tz(args.tz))
    rep = daynight.analyze(conn, cfg, realm=args.realm, faction=args.faction, since=_since(args.days))
    listed = rep.listed(cfg)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["realm", "faction", "key", "name", "nights", "night_vs_day_pct", "qty_night_vs_day_pct",
                        "nights_cheaper", "nights_pricier", "sign_test_p", f"day_{args.metric}_copper",
                        f"night_{args.metric}_copper", "day_qty", "night_qty", "nightly_pct", "listed"])
            for r in sorted(rep.items, key=lambda r: r.effect):
                w.writerow([r.realm, r.faction, r.key, r.name, r.nights, round(r.effect * 100, 2),
                            round(r.qty_effect * 100, 2), r.lower, r.higher, round(r.p_value, 4),
                            round(r.day_price), round(r.night_price), r.day_qty, r.night_qty,
                            " ".join(f"{e * 100:+.1f}" for e in r.nightly), r in listed])
        print(f"Wrote {len(rep.items)} items to {args.csv}")

    print(f"Night = hours {sorted(night)}, day = hours {sorted(day)}. Each night is compared with the average of "
          f"the day before and the day after, so a steady price trend cancels out.")
    print(f"Nights with a day on both sides: {len(rep.nights)}"
          + (f" ({', '.join(rep.nights)})" if rep.nights else ""))
    if len(rep.nights) < args.min_nights:
        print(f"\nNot enough yet: an item needs {args.min_nights} such nights (--min-nights). Each one needs a "
              "snapshot in the day window, then the night window, then the next day window. "
              "Check coverage with 'snapshots'.")
        return
    if not rep.items:
        print("\nNo item has enough units listed on enough nights. Try --min-qty 2 or --min-nights 2.")
        return

    print(f"\nWhole market ({len(rep.items):,} items with at least {args.min_qty:g} listed, metric {args.metric}):")
    print(f"  price at night vs day : {_pct(rep.market_price)} typical item "
          f"({rep.share_lower:.0%} of item-nights >2% cheaper, {rep.share_higher:.0%} >2% pricier)")
    print(f"  units listed at night : {_pct(rep.market_qty)}")
    print(f"  price trend           : {_pct(rep.trend_per_day)} per day (day to next day)")

    print(f"\nItems at least {args.min_effect:g}% different at night on a median night, "
          f"with at least {args.agree:g}% of nights agreeing: {len(listed)}")
    if rep.chance_listed is not None:
        print(f"  By chance alone you'd expect about {rep.chance_listed:.0f} items to pass these filters "
              f"(night and day randomly swapped).")
        if len(listed) <= 1.5 * rep.chance_listed:
            print("  That's close to what you have, so most of the items below are probably luck. "
                  "More nights will tell them apart.")
    multi = len({(r.realm, r.faction) for r in listed}) > 1

    def show(title, subset):
        if not subset:
            return
        print(f"\n{title}")
        headers = (["realm"] if multi else []) + [
            "item", "day", "night", "night vs day", "each night", "qty night vs day", "nights", "p"]
        _table(headers, [
            ([f"{r.realm}-{r.faction}"] if multi else []) + [
                r.name[:38], format_money(r.day_price), format_money(r.night_price), _pct(r.effect),
                " ".join(_short_pct(e) for e in r.nightly[-6:]), _pct(r.qty_effect),
                f"{r.agreeing}/{r.nights}", f"{r.p_value:.2f}"]
            for r in subset], ("l" if multi else "") + "lrrrrrrr")

    cheaper = sorted((r for r in listed if r.effect < 0), key=lambda r: r.effect)[:args.top]
    pricier = sorted((r for r in listed if r.effect > 0), key=lambda r: -r.effect)[:args.top]
    show(f"Cheaper at night (buy at night), {len(cheaper)} shown:", cheaper)
    show(f"Pricier at night (sell at night), {len(pricier)} shown:", pricier)
    print("\n  each night: that night's price change in %, oldest first (last 6 shown).")
    print("  nights: how many nights agree out of those measured. p: sign test; with only a few nights it "
          "can't go below ~0.25, so look at agreement and the chance estimate above.")


def _deal_settings(args) -> deals.Settings:
    return deals.Settings(
        discount=args.discount / 100, min_seen=args.min_seen, min_profit=int(args.min_profit * 10000),
        min_usual=int(args.min_price * 10000), max_spread=None if args.max_spread < 0 else args.max_spread,
        history_seconds=int(args.history * 86400), include_stale=args.all)


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} days ago"


def cmd_deals(args, conn):
    cfg = _deal_settings(args)
    if args.backtest:
        bt = deals.backtest(conn, cfg, realm=args.realm, faction=args.faction, since=_since(args.days))
        if not bt.deals:
            print("No deals to check: need snapshots with another one taken within 3 hours after them.")
            return
        pct = lambda n: f"{n / bt.deals:.0%}"  # noqa: E731
        print(f"Replayed {bt.snapshots} snapshots that had a next snapshot within 3 hours.")
        print(f"  {bt.deals} deals flagged ({bt.deals / bt.snapshots:.1f} per snapshot)")
        print(f"  gone by the next snapshot : {bt.gone} ({pct(bt.gone)}), "
              f"worth ~{format_money(bt.profit_gone)} estimated profit")
        print(f"  partly gone               : {bt.partly} ({pct(bt.partly)})")
        print(f"  still there               : {bt.still} ({pct(bt.still)})")
        if bt.normal:
            print(f"  for comparison, a normally priced cheapest listing was gone "
                  f"{bt.normal_gone / bt.normal:.0%} of the time")
        print("'Gone' means bought, cancelled or expired; a snapshot can't tell which. The further the deal rate "
              "is above the normal rate, the more other players are treating these as real deals.")
        return

    found = deals.find_deals(conn, cfg, realm=args.realm, faction=args.faction)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["realm", "faction", "key", "name", "price_copper", "usual_copper", "discount_pct",
                        "units", "buy_at_copper", "est_profit_copper", "quantity", "usual_quantity",
                        "seen", "history", "spread", "dip_rate", "cheap_for_snapshots"])
            for d in found:
                w.writerow([d.realm, d.faction, d.key, d.name, d.price, round(d.usual), round(d.discount_pct, 1),
                            d.units, d.buy_at, round(d.profit), d.quantity, d.usual_qty, d.seen, d.history,
                            round(d.spread, 3), round(d.dip_rate, 3), d.cheap_for])
        print(f"Wrote {len(found)} deals to {args.csv}")
    if not found:
        print("No deals in the newest snapshot with these settings. Try --discount 20 or --min-profit 0.5.")
        return

    import time
    newest = max(d.ts for d in found)
    print(f"Deals in the snapshot from {analysis.local_dt(newest, None):%a %H:%M} ({_ago(time.time() - newest)}), "
          f"judged against the previous {args.history:g} days."
          + ("" if args.all else " Only listings that are new since the previous snapshot (--all shows every one).")
          + "\n")
    multi = len({(d.realm, d.faction) for d in found}) > 1
    headers = (["realm"] if multi else []) + [
        "item", "now", "usual", "off", "buy", "resell at", "est. profit", "qty now/usual", "jumpy",
        "this low before"] + (["cheap for"] if args.all else [])
    _table(headers, [
        ([f"{d.realm}-{d.faction}"] if multi else []) + [
            d.name[:38], format_money(d.price), format_money(d.usual), f"-{d.discount_pct:.0f}%",
            f"{d.units} at <= {format_money(d.buy_at)}" if d.units > 1 else "1",
            format_money(d.resale), format_money(d.profit), f"{d.quantity}/{d.usual_qty:g}", f"{d.spread:.2f}",
            f"{d.dip_rate:.0%} of {d.seen}"]
        + (["new" if d.cheap_for == 0 else f"{d.cheap_for} snaps"] if args.all else [])
        for d in found[:args.top]], ("l" if multi else "") + "l" + "r" * (len(headers) - 1 - multi))
    print(f"\n{len(found)} deals, top {min(len(found), args.top)} by estimated profit.")
    print("  usual: typical price of the cheapest listings in earlier snapshots.")
    print("  buy: at least this many units are listed at or below that price.")
    print("  resell at: the usual price, or the next listing up if that's cheaper. Profit = buying those units "
          "and relisting there, minus the 5% AH cut. Deposits aren't counted.")
    print("  jumpy: how much the usual price swings (0 = steady; 0.5 = the middle half spans 50% of it).")
    print("  this low before: how often earlier snapshots had a price this low. High = it's often this cheap.")
    if args.all:
        print("  cheap for: snapshots in a row it's already been this cheap. A cheap listing nobody buys "
              "for hours is usually a price drop, not a deal.")
    print("These are asking prices, not sales: a snapshot can't see what actually sells. "
          "'deals --backtest' shows how often flagged deals disappear soon after.")


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

    s = sub.add_parser("import", parents=[common],
                       help="import snapshots from this PC's game files and the shared folder")
    s.add_argument("--file", action="append", help="path to AHSnapshot.lua (default: auto-detect)")
    s.add_argument("--from", dest="from_dir", help="shared folder to read (default: configured one)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--local-only", action="store_true", help="only this PC's game files")
    g.add_argument("--sync-only", action="store_true", help="only the shared folder")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("push", parents=[common], help="copy this PC's new snapshots to the shared folder")
    s.add_argument("--file", action="append", help="path to AHSnapshot.lua (default: auto-detect)")
    s.add_argument("--to", help="shared folder (default: configured one)")
    s.set_defaults(func=cmd_push)

    s = sub.add_parser("config", parents=[common], help="show or set this PC's settings")
    s.add_argument("--sync-dir", help=r"shared folder for push/import, e.g. \\nas\share\AHSnapshot "
                                      "('' to unset)")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("snapshots", parents=[common], help="list imported snapshots and hour-of-day coverage")
    s.set_defaults(func=cmd_snapshots)

    s = sub.add_parser("daynight", parents=[common],
                       help="items whose price differs at night, each night compared with the days around it")
    s.add_argument("--night", default="22-6", help="night hours, end exclusive (default: %(default)s)")
    s.add_argument("--day", default="9-18", help="day hours, end exclusive (default: %(default)s)")
    s.add_argument("--metric", choices=store.METRICS, default="p10",
                   help="price per snapshot: min, or quantity-weighted p10/p25/p50 (default: %(default)s)")
    s.add_argument("--min-nights", type=int, default=3,
                   help="nights (with a day before and after) an item must be measured on (default: %(default)s)")
    s.add_argument("--min-qty", type=float, default=5,
                   help="median units listed in each window, to skip thin items (default: %(default)s)")
    s.add_argument("--min-price", type=float, default=0, help="ignore items cheaper than this many gold "
                                                                 "(default: %(default)s)")
    s.add_argument("--min-effect", type=float, default=5,
                   help="list items whose median nightly change is at least this %% (default: %(default)s)")
    s.add_argument("--agree", type=float, default=80,
                   help="%% of nights that must point the same way (default: %(default)s)")
    s.add_argument("--top", type=int, default=20, help="rows per list (default: %(default)s)")
    s.add_argument("--csv", help="also write every measured item to this CSV")
    s.set_defaults(func=cmd_daynight)

    s = sub.add_parser("deals", parents=[common],
                       help="listings in the newest snapshot well below the item's usual price")
    s.add_argument("--discount", type=float, default=30,
                   help="flag listings at least this %% below the usual price (default: %(default)s)")
    s.add_argument("--min-profit", type=float, default=1,
                   help="minimum estimated profit in gold (default: %(default)s)")
    s.add_argument("--min-price", type=float, default=0,
                   help="ignore items usually cheaper than this many gold (default: %(default)s)")
    s.add_argument("--min-seen", type=int, default=6,
                   help="earlier snapshots the item must appear in (default: %(default)s)")
    s.add_argument("--max-spread", type=float, default=0.5,
                   help="skip items whose usual price is jumpier than this; -1 = no limit (default: %(default)s)")
    s.add_argument("--history", type=float, default=14,
                   help="days of earlier snapshots that set the usual price (default: %(default)s)")
    s.add_argument("--all", action="store_true",
                   help="also show listings that were already this cheap in the previous snapshot")
    s.add_argument("--top", type=int, default=30, help="rows to show (default: %(default)s)")
    s.add_argument("--csv", help="also write all deals to this CSV")
    s.add_argument("--backtest", action="store_true",
                   help="replay past snapshots: how many flagged deals were gone by the next snapshot?")
    s.set_defaults(func=cmd_deals)

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
