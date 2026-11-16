# AHSnapshot

Two parts:

| Part | What it does |
|---|---|
| `addon/AHSnapshot/` | WoW: Forever addon. Every time you're at the auction house it records a price snapshot of the **entire** AH (one full scan). |
| `analyzer/` | Python tool (no dependencies, Python 3.10+). Imports those snapshots into a SQLite database and analyzes them, e.g. which items are cheaper or pricier at night than during the day. |

## 1. Install the addon

Copy the `addon/AHSnapshot` folder to:

```
C:\Program Files (x86)\World of Warcraft\_forever_\Interface\AddOns\AHSnapshot
```

It targets the Forever client (`## Interface: 16001`, modern `C_AuctionHouse` API, same approach Auctionator uses).

## 2. Collect data in game

- **Open the auction house.** After about 2 seconds the addon requests a full scan and records it. Chat shows `Saved snapshot #N: … auctions, … items`.
- **The server allows one full scan per account every 15 minutes.** If you reopen the AH sooner, the addon tells you when the next one is allowed.
- **Auctionator compatibility.** Auctionator also does a full scan on AH open. AHSnapshot sees Auctionator's scan and records that data too, instead of competing for the cooldown. You get a snapshot either way.
- **The "Snapshot" button** below the AH window starts a scan manually. Hover over it for status.
- **Keep the AH open** for a few seconds until the "Saved snapshot" message appears.
- **Data is written to disk only when you log out or `/reload`.**

| Command | |
|---|---|
| `/ahs` or `/ahs status` | scan progress, snapshot count, last snapshot, cooldown |
| `/ahs scan` | scan now (if the cooldown allows) |
| `/ahs cancel` | abandon the snapshot being recorded |
| `/ahs auto on\|off` | toggle the automatic scan on AH open |
| `/ahs max <n>` | how many snapshots to keep in the game file (default 30, oldest dropped first; each is ~1 MB there, so move them out with `import`/`push`) |
| `/ahs clear yes` | delete stored snapshots (import them first!) |

**What's stored per snapshot:** time, realm, faction, and for each item: number of auctions, total quantity, and unit buyout prices (min, 10th/25th/50th percentile weighted by quantity, max).
- Random-suffix gear ("of the Bear" / "of the Monkey") is tracked separately, keyed as `itemID:<suffix>` (e.g. `15216:of the Bear`). The suffix comes from the item's name, because Forever item links don't carry a suffix ID.
- Snapshots recorded by addon version 0.1 merged all suffixes of an item together; the analyzer skips them on import.
- A snapshot is roughly 30 bytes per item, so a few hundred KB.
- The analyzer's database keeps the long-term history. Import regularly and you can keep the in-game file small.

## 3. Analyze

From the `analyzer` folder (or pass the full path to `ahs.py`):

```bash
python ahs.py import
```

This auto-finds `…\_forever_\WTF\Account\*\SavedVariables\AHSnapshot.lua` and is safe to run repeatedly, because already-imported snapshots are skipped. Data goes into `analyzer/ahsnapshot.sqlite`.

```bash
python ahs.py snapshots
```

Lists snapshots and shows how many you have at each hour of the day. Day/night comparisons need visits at both times.

```bash
python ahs.py daynight
```

Shows which items are cheaper at night (buy then) and which are pricier at night (sell then). Useful options:
- `--night 23-7` and `--day 10-22`: set the windows (local hours, end exclusive). By default, day is every hour that isn't night.
- `--metric min|p10|p25|p50`: which price to compare. The default is `p10`, which is less noisy than the single cheapest auction.
- `--max-p 0.05`: hide differences that are likely noise.
- `--min-obs` / `--min-days`: require more data before an item is compared.
- `--csv out.csv`: also write the full comparison.
- `--realm`, `--faction`, `--days 14`, `--tz America/New_York`: filters and time zone.

```bash
python ahs.py hourly "Black Lotus"
```

One item's price and supply at each hour of the day.

```bash
python ahs.py history "Runecloth"
```

One item's prices in every snapshot.

```bash
python ahs.py export all.csv
```

Everything as one CSV (with local hour and weekday columns) for Excel or pandas.

**How to read the day/night table:** it compares the median per-snapshot price in each window. The `p` column is a Mann-Whitney test. Snapshots taken close together aren't independent, so treat `p` as a guide, not proof. A few weeks of visits at varied hours gives much more reliable results than a few days.

## 4. Several gaming PCs

WoW keeps addon data on each PC; nothing syncs it. So each gaming PC copies its snapshots to a shared folder (a network share or NAS), and the PC that runs the analysis imports from there.

- **Shared folder:** each snapshot becomes its own compressed file (about 350 KB), named by realm, faction and time. PCs never overwrite each other.
- **Repeats are safe:** pushing or importing again never duplicates anything.
- **The SQLite database stays local** on the analysis PC. Don't put it on the network share.

**Set up each gaming PC (once):**
1. Install Python 3.10+ and clone this repo.
2. Copy `addon/AHSnapshot` into that PC's `_forever_\Interface\AddOns`.
3. Point it at the shared folder:

```bash
python analyzer/ahs.py config --sync-dir "\\YOUR-NAS\share\AHSnapshot"
```

Run that from PowerShell or Command Prompt. Git Bash strips one of the two leading backslashes; if you use Git Bash, write the path with forward slashes instead: `//YOUR-NAS/share/AHSnapshot`. Run `python analyzer/ahs.py config` afterwards to check that it says `reachable: yes`.

**After playing on a gaming PC:** `/reload` or log out, then:

```bash
python analyzer/ahs.py push
```

Once pushed, `/ahs clear yes` in game is safe if you want to shrink the game file. The addon keeps the newest 30 snapshots by default.

**On the analysis PC:** set the same `--sync-dir` once. After that, `import` reads the shared folder and this PC's own game files:

```bash
python analyzer/ahs.py import
```

Use `--sync-only` or `--local-only` to limit it to one source, and `--from <folder>` / `--to <folder>` to use a folder other than the configured one. `python analyzer/ahs.py config` shows the current setting and whether the folder is reachable. The setting is stored in `analyzer/ahs_config.json` (not committed); the `AHS_SYNC_DIR` environment variable overrides it.

## Tests

`tests/addon_sim.py` runs the real addon Lua under LuaJIT with a mocked WoW API (`pip install lupa`). It checks:
- Simulated AH visits over several days are recorded.
- The 15-minute cooldown is respected.
- Scans that Auctionator starts are still recorded.
- Items load late, bid-only auctions, nonexistent items, and suffix gear are handled.
- Percentiles match an independent calculation.

`tests/sync_test.py` simulates two gaming PCs pushing to a shared folder and checks that the analysis PC's import has no duplicates, handles corrupt or half-copied files, and matches a direct import exactly.

The addon simulation also writes a SavedVariables file with planted day/night price patterns:

```bash
python tests/addon_sim.py --out sim/AHSnapshot.lua
```

```bash
python analyzer/ahs.py import --file sim/AHSnapshot.lua --db sim/test.sqlite
```

```bash
python analyzer/ahs.py daynight --db sim/test.sqlite
```

The simulation is not the live client. The first in-game run is the real test, and the button position is a guess.
