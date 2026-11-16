"""Run the real addon Lua under LuaJIT with a mocked WoW API, simulating AH visits.

Requires `pip install lupa` (only for this test; the analyzer has no dependencies).

    python tests/addon_sim.py --out sim/AHSnapshot.lua

Simulates several days of AH visits with known price patterns, then writes the
addon's SavedVariables in WoW's on-disk format so the analyzer can import it.
"""

from __future__ import annotations

import argparse
import importlib
import random
from datetime import datetime, timedelta
from pathlib import Path

import lupa.luajit21 as lupa

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "addon" / "AHSnapshot" / "AHSnapshot.lua"

MOCK = r"""
SIM = { clock = 0, epoch = 0, timers = {}, frames = {}, auctions = {}, loaded = {}, log = {},
        replicateCalls = 0, replicateDelay = 3 }

function GetTime() return SIM.clock end
function time(t) if t then return os.time(t) end return SIM.epoch + math.floor(SIM.clock) end
function GetServerTime() return SIM.epoch + math.floor(SIM.clock) end
date = os.date
difftime = os.difftime
function print(msg) table.insert(SIM.log, msg) end
function GetRealmName() return "Testrealm" end
function UnitFactionGroup() return "Horde" end

C_Timer = {}
function C_Timer.After(delay, fn) table.insert(SIM.timers, { at = SIM.clock + delay, fn = fn }) end

local FrameMT = {}
FrameMT.__index = FrameMT
function FrameMT:RegisterEvent(e) self.events[e] = true end
function FrameMT:UnregisterEvent(e) self.events[e] = nil end
function FrameMT:SetScript(k, fn) self.scripts[k] = fn end
function FrameMT:SetSize() end
function FrameMT:SetPoint() end
function FrameMT:SetText(t) self.text = t end
function FrameMT:Enable() self.enabled = true end
function FrameMT:Disable() self.enabled = false end
function CreateFrame(kind, name, parent)
  local f = setmetatable({ events = {}, scripts = {} }, FrameMT)
  table.insert(SIM.frames, f)
  if name then _G[name] = f end
  return f
end
AuctionHouseFrame = CreateFrame("Frame")
GameTooltip = CreateFrame("Frame")
function GameTooltip:SetOwner() end
function GameTooltip:AddLine() end
function GameTooltip:Show() end
SlashCmdList = {}

-- Every callback runs like WoW runs it: errors abort only that callback (and are
-- collected in SIM.errors). We track the most Lua time spent in any one frame,
-- since that's what the player feels as a hitch.
SIM.errors, SIM.maxCallback, SIM.cpu = {}, 0, 0
local frameCPU = 0
local function Invoke(fn, ...)
  local c = os.clock()
  local ok, err = pcall(fn, ...)
  local d = os.clock() - c
  SIM.cpu = SIM.cpu + d
  frameCPU = frameCPU + d
  if not ok then table.insert(SIM.errors, tostring(err)) end
end

function SIM.fire(event, ...)
  for _, f in ipairs(SIM.frames) do
    if f.events[event] and f.scripts.OnEvent then Invoke(f.scripts.OnEvent, f, event, ...) end
  end
end

-- Frame-by-frame: each frame runs every timer that is due; a timer created during
-- a frame (even with 0 delay) runs on a later frame.
local FRAME = 1 / 60
function SIM.run(maxSeconds)
  local limit = SIM.clock + maxSeconds
  while true do
    local due, later = {}, {}
    for _, t in ipairs(SIM.timers) do
      if t.at <= SIM.clock then due[#due + 1] = t else later[#later + 1] = t end
    end
    SIM.timers = later
    table.sort(due, function(a, b) return a.at < b.at end)
    frameCPU = 0
    for _, t in ipairs(due) do Invoke(t.fn) end
    if frameCPU > SIM.maxCallback then SIM.maxCallback = frameCPU end

    if #SIM.timers == 0 then break end
    local nextAt = math.huge
    for _, t in ipairs(SIM.timers) do if t.at < nextAt then nextAt = t.at end end
    local nextClock = math.max(SIM.clock + FRAME, nextAt)
    if nextClock > limit then break end
    SIM.clock = nextClock
  end
  SIM.clock = math.max(SIM.clock, limit)
end

C_Item = {}
SIM.baseNames = {}
function C_Item.DoesItemExistByID(id) return id ~= 999999 end
-- Like the real client: the base item's info is cached separately from an auction's
-- item; the first lookup returns nothing and starts loading it.
SIM.baseLoaded, SIM.baseLoading = {}, {}
function C_Item.GetItemInfo(id)
  if SIM.baseLoaded[id] then return SIM.baseNames[id] end
  if not SIM.baseLoading[id] then
    SIM.baseLoading[id] = true
    C_Timer.After(0.3, function() SIM.baseLoaded[id] = true end)
  end
end
function C_Item.RequestLoadItemDataByID(id)
  C_Timer.After(0.3, function() SIM.loaded[id] = true end)
end

C_AuctionHouse = {}
function C_AuctionHouse.ReplicateItems()
  SIM.replicateCalls = SIM.replicateCalls + 1
  C_Timer.After(SIM.replicateDelay, function() SIM.fire("REPLICATE_ITEM_LIST_UPDATE") end)
end
function C_AuctionHouse.GetNumReplicateItems() return #SIM.auctions end
function C_AuctionHouse.GetReplicateItemInfo(i)
  local a = SIM.auctions[i + 1]
  if not a then return end
  local ok = SIM.loaded[a.itemID] or false
  return ok and a.name or nil, 134400, a.count, a.quality, true, 1, 0, a.buyout, 1, a.buyout,
         0, false, nil, nil, nil, 0, a.itemID, ok
end
function C_AuctionHouse.GetReplicateItemLink(i)
  local a = SIM.auctions[i + 1]
  if not a or not SIM.loaded[a.itemID] then return nil end
  return a.link
end
"""

# itemID, name, quality, base unit price (copper), night price factor, stack sizes, suffix names
ITEMS = [
    (13468, "Black Lotus", 2, 45_0000, 0.80, [1], None),                 # 20% cheaper at night
    (12359, "Thorium Bar", 1, 1_5000, 1.00, [1, 5, 10, 20], None),       # flat
    (14047, "Runecloth", 1, 2500, 1.18, [1, 10, 20], None),              # 18% pricier at night
    (13926, "Golden Pearl", 2, 8_0000, 1.00, [1], None),                 # flat
    (15216, "Ornate Mithril Boots", 2, 30_0000, 1.00, [1], {1017: "of the Bear", 1205: "of the Monkey"}),
]


def make_auctions(rng: random.Random, night: bool, gear_suffix_price: dict[int, float]) -> list[dict]:
    auctions = []
    for item_id, name, quality, base, night_factor, stacks, suffixes in ITEMS:
        factor = night_factor if night else 1.0
        for _ in range(rng.randint(6, 20)):
            count = rng.choice(stacks)
            unit = base * factor * rng.uniform(0.95, 1.30)
            suffix = 0
            full_name = name
            if suffixes:
                suffix = rng.choice(list(suffixes))
                full_name = f"{name} {suffixes[suffix]}"
                unit *= gear_suffix_price[suffix]
            link = (f"|cff1eff00|Hitem:{item_id}::::::{suffix}:0:60:::::::::|h[{full_name}]|h|r")
            buyout = round(unit * count)
            if rng.random() < 0.05:
                buyout = 0  # bid-only auction
            auctions.append(dict(itemID=item_id, name=name, quality=quality, count=count,
                                 buyout=buyout, link=link))
    # Blizzard glitch: an entry for an item that doesn't exist
    auctions.append(dict(itemID=999999, name="Ghost", quality=0, count=1, buyout=100, link=None))
    rng.shuffle(auctions)
    return auctions


def to_lua(lua, value):
    if isinstance(value, dict):
        return lua.table_from({k: to_lua(lua, v) for k, v in value.items() if v is not None})
    if isinstance(value, list):
        return lua.table_from([to_lua(lua, v) for v in value])
    return value


def serialize(value, indent: int = 0) -> str:
    """Write a Lua value the way WoW writes SavedVariables."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(int(value)) if float(value).is_integer() else repr(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    if lupa.lua_type(value) == "table":
        keys = list(value.keys())
        n = len(value)
        out = ["{"]
        for i in range(1, n + 1):
            out.append(f"{serialize(value[i], indent + 1)}, -- [{i}]")
        for k in keys:
            if isinstance(k, int) and 1 <= k <= n:
                continue
            key = f"[{k}]" if isinstance(k, (int, float)) else f'["{k}"]'
            out.append(f"{key} = {serialize(value[k], indent + 1)},")
        out.append("}")
        return "\n".join(out)
    raise TypeError(type(value))


def new_runtime():
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(MOCK)
    sim = lua.globals().SIM
    lua.execute(ADDON.read_text(encoding="utf-8").replace("local ADDON_NAME = ...", 'local ADDON_NAME = "AHSnapshot"'))
    sim.fire("ADDON_LOADED", "AHSnapshot")
    return lua, sim


STRESS_MOCK = r"""
-- Item data arrives gradually, like a real server answering thousands of requests.
local queue = 0
function C_Item.RequestLoadItemDataByID(id)
  queue = queue + 1
  C_Timer.After(0.3 + queue * SIM.loadSpacing, function() SIM.loaded[id] = true end)
end

function SIM.buildAuctions(n, distinct, cachedFraction, seed)
  math.randomseed(seed)
  local auctions, loaded = {}, {}
  for i = 1, n do
    local id = 1000 + math.random(distinct)
    local count = math.random(5) == 1 and math.random(20) or 1
    local unit = math.floor(10 ^ (2 + math.random() * 4))           -- 1c .. 100g, log-uniform
    if math.random(2000) == 1 then unit = 99999990000 end            -- 9,999,999g troll listing
    local suffix = (id % 7 == 0) and (1000 + math.random(30)) or 0
    auctions[i] = { itemID = id, name = "Item " .. id, quality = 2, count = count,
                    buyout = math.random(20) == 1 and 0 or unit * count,
                    link = "|cff1eff00|Hitem:" .. id .. "::::::" .. suffix .. ":0:60:::::::::|h[Item " .. id .. "]|h|r" }
    loaded[id] = (id % 100) < cachedFraction * 100
    SIM.baseNames[id] = "Item " .. id
  end
  SIM.auctions, SIM.loaded = auctions, loaded
end
"""


def stress(n: int, distinct: int, cached: float, spacing: float) -> None:
    lua, sim = new_runtime()
    lua.execute(STRESS_MOCK)
    sim.loadSpacing = spacing
    sim.epoch = int(datetime.now().timestamp())
    sim.buildAuctions(n, distinct, cached, 1)
    sim.fire("AUCTION_HOUSE_SHOW")
    started = None
    t = 0.0
    while t < 600:
        sim.run(1)
        t += 1
        if started is None and sim.replicateCalls > 0:
            started = t
        if len(lua.globals().AHSnapshotDB.snapshots) > 0 or len(sim.errors) > 0:
            break
    log = [l.replace("|cff33ff99", "").replace("|r", "") for l in sim.log.values()]
    print("\n".join("  " + l for l in log))
    print(f"\n{n:,} auctions, {distinct:,} distinct items, {cached:.0%} cached")
    print(f"  game time to finish : ~{t:.0f}s")
    print(f"  total Lua CPU       : {sim.cpu:.2f}s")
    print(f"  busiest frame (Lua) : {sim.maxCallback * 1000:.0f} ms")
    for e in list(sim.errors.values())[:5]:
        print(f"  ERROR: {e}")
    snaps = lua.globals().AHSnapshotDB.snapshots
    if len(snaps):
        items = snaps[1]["items"]
        bad = [v for v in items.values() if "-" in v]
        print(f"  items saved         : {len(list(items.keys())):,}  (negative values: {len(bad)})")


def main() -> None:
    global lupa
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "sim" / "AHSnapshot.lua"))
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--lua", default="luajit21", choices=["luajit21", "lua51"],
                    help="Lua runtime; lua51 is closest to WoW's interpreter speed")
    ap.add_argument("--stress", type=int, metavar="N", help="instead: one scan of N auctions, timed")
    ap.add_argument("--distinct", type=int, default=9000)
    ap.add_argument("--cached", type=float, default=0.2, help="fraction of items already in the item cache")
    ap.add_argument("--load-spacing", type=float, default=0.002, help="seconds between item-data answers")
    args = ap.parse_args()

    lupa = importlib.import_module(f"lupa.{args.lua}")
    if args.stress:
        stress(args.stress, args.distinct, args.cached, args.load_spacing)
        return

    rng = random.Random(args.seed)

    # Upgrade path: a 0.2.x file with the old 150-snapshot default drops to 30.
    old = lupa.LuaRuntime(unpack_returned_tuples=True)
    old.execute(MOCK)
    old.execute('AHSnapshotDB = { version = 2, settings = { autoScan = true, maxSnapshots = 150 }, '
                'items = {}, snapshots = {} }')
    old.execute(ADDON.read_text(encoding="utf-8").replace("local ADDON_NAME = ...", 'local ADDON_NAME = "AHSnapshot"'))
    old.globals().SIM.fire("ADDON_LOADED", "AHSnapshot")
    upgraded = old.globals().AHSnapshotDB
    upgrade_ok = upgraded.settings.maxSnapshots == 30 and upgraded.version == 3

    lua, sim = new_runtime()
    lua.globals().SlashCmdList.AHSNAPSHOT("max 500")  # keep every simulated visit

    gear_price = {1017: 1.0, 1205: 1.6}  # "of the Monkey" sells for more
    sim.baseNames = lua.table_from({item_id: name for item_id, name, *_ in ITEMS})
    start = datetime.now().replace(minute=0, second=0, microsecond=0) - timedelta(days=args.days)
    visits = []
    t = start
    while t < start + timedelta(days=args.days):
        visits.append(t)
        t += timedelta(hours=rng.choice([2, 3, 4]), minutes=rng.randint(0, 50))

    checks = [("old 150-snapshot limit upgraded to 30", upgrade_ok)]
    for i, when in enumerate(visits):
        sim.epoch = int(when.timestamp()) - int(sim.clock)
        night = when.hour >= 22 or when.hour < 6
        sim.auctions = to_lua(lua, make_auctions(rng, night, gear_price))
        sim.loaded = lua.table_from({13926: True, 15216: True, 12359: True})  # others need loading
        before = len(lua.globals().AHSnapshotDB.snapshots)

        if i == 2:
            # Auctionator started the scan itself: we must not call ReplicateItems, but still record it.
            lua.execute("Auctionator = { SavedState = { TimeOfLastReplicateScan = time() } }")
            calls = sim.replicateCalls
            sim.fire("AUCTION_HOUSE_SHOW")
            sim.run(1)
            lua.globals().C_AuctionHouse.ReplicateItems()  # Auctionator's call
            sim.run(60)
            checks.append(("auctionator-triggered scan recorded",
                           len(lua.globals().AHSnapshotDB.snapshots) == before + 1
                           and sim.replicateCalls == calls + 1))
            lua.execute("Auctionator = nil")
        else:
            sim.fire("AUCTION_HOUSE_SHOW")
            sim.run(60)
            checks.append((f"visit {i} recorded", len(lua.globals().AHSnapshotDB.snapshots) == before + 1))

        if i == 0:
            # Re-open within 15 minutes: must not call ReplicateItems again.
            sim.fire("AUCTION_HOUSE_CLOSED")
            calls = sim.replicateCalls
            sim.run(120)
            sim.fire("AUCTION_HOUSE_SHOW")
            sim.run(30)
            checks.append(("cooldown respected", sim.replicateCalls == calls))
        sim.fire("AUCTION_HOUSE_CLOSED")
        sim.run(16 * 60)

    # Slash commands shouldn't error.
    for cmd in ["status", "auto", "auto on", "max 500", "clear", "help"]:
        lua.globals().SlashCmdList.AHSNAPSHOT(cmd)

    # Cross-check one snapshot's stats against an independent Python computation.
    db = lua.globals().AHSnapshotDB
    last = db.snapshots[len(db.snapshots)]
    auctions = [dict(a) for a in sim.auctions.values()]
    bear = [a for a in auctions if a["itemID"] == 15216 and ":1017:" in a["link"] and a["buyout"] > 0]
    units = sorted(a["buyout"] / a["count"] for a in bear)
    encoded = last["items"]["15216:of the Bear"].split(",")
    checks.append(("suffix item keyed separately", "15216:of the Monkey" in list(last["items"].keys())
                   and "15216" not in list(last["items"].keys())))
    checks.append(("snapshot tagged with key scheme 3", last["kv"] == 3 and db["version"] == 3))
    first = db.snapshots[1]["items"]
    checks.append(("no merged plain key for suffix item, even on first scan", "15216" not in list(first.keys())))
    checks.append(("single-auction items named", all(k in list(db["items"].keys()) for k in first.keys())))
    checks.append(("min matches", int(encoded[2]) == round(units[0])))
    checks.append(("p50 matches", int(encoded[5]) == round(units[(len(units) + 1) // 2 - 1])))
    checks.append(("names captured", db["items"]["15216:of the Monkey"]["n"] == "Ornate Mithril Boots of the Monkey"
                   and db["items"]["13468"]["n"] == "Black Lotus"))
    checks.append(("late-loading items recorded", "13468" in list(last["items"].keys())))
    checks.append(("ghost item skipped", "999999" not in list(last["items"].keys())))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\nAHSnapshotDB = " + serialize(db) + "\n", encoding="utf-8")

    log = list(sim.log.values())
    print("Addon chat output (first 12 lines):")
    for line in log[:12]:
        print("  ", line.replace("|cff33ff99", "").replace("|r", ""))
    print(f"\n{len(visits)} visits simulated, {len(db.snapshots)} snapshots saved -> {out}")
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if failed:
        raise SystemExit(f"{len(failed)} checks failed")


if __name__ == "__main__":
    main()
