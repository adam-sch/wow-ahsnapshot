"""End-to-end test of multi-PC syncing: two simulated gaming PCs push to a shared
folder, and the analysis PC imports from it.

    python tests/sync_test.py      (needs `pip install lupa` for the addon simulation)
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "analyzer"))
from ahsnap import store, sync  # noqa: E402


def make_pc(tmp: Path, name: str, seed: int) -> Path:
    """Run the addon simulation as one PC; returns its SavedVariables file."""
    sv = tmp / name / "WTF" / "Account" / "ACCT" / "SavedVariables" / "AHSnapshot.lua"
    subprocess.run([sys.executable, str(ROOT / "tests" / "addon_sim.py"), "--lua", "lua51",
                    "--seed", str(seed), "--days", "2", "--out", str(sv)],
                   check=True, capture_output=True)
    return sv


def table(conn: sqlite3.Connection, sql: str):
    return sorted(tuple(r) for r in conn.execute(sql))


def main() -> None:
    checks = []
    tmp = Path(tempfile.mkdtemp(prefix="ahs-sync-"))
    try:
        share = tmp / "share"
        share.mkdir()
        pc_a, pc_b = make_pc(tmp, "pc-a", 11), make_pc(tmp, "pc-b", 12)
        ids_a = {store.snapshot_id(x) for x in store.read_saved_variables(pc_a)[0]}
        ids_b = {store.snapshot_id(x) for x in store.read_saved_variables(pc_b)[0]}
        n_a, n_b = len(ids_a), len(ids_b)
        overlap = len(ids_a & ids_b)  # same realm, faction and second = the same snapshot
        total = len(ids_a | ids_b)

        r = sync.push_file(pc_a, share, machine="PC-A")
        checks.append(("PC-A pushes all its snapshots", r.written == n_a))
        r = sync.push_file(pc_a, share, machine="PC-A")
        checks.append(("pushing again copies nothing", r.written == 0 and r.already_there == n_a))
        r = sync.push_file(pc_b, share, machine="PC-B")
        checks.append(("PC-B pushes its snapshots alongside", r.written == n_b - overlap))
        checks.append((f"identical snapshots from both PCs stored once ({overlap} here)",
                       r.already_there == overlap and overlap > 0))
        files = list(share.glob("*" + sync.SUFFIX))
        checks.append(("one file per snapshot, no leftovers", len(files) == total
                       and not list(share.glob("*.tmp"))))

        # a half-copied temp file from another PC must be ignored; a corrupt file reported and retried
        (share / "Testrealm_Horde_1.json.gz.PC-C.tmp").write_bytes(b"partial")
        bad = share / "Testrealm_Horde_2.json.gz"
        bad.write_bytes(b"not gzip")

        conn = store.connect(tmp / "analysis.sqlite")
        r = sync.import_folder(conn, share)
        checks.append(("analysis PC imports both PCs' snapshots", r.imported == total))
        checks.append(("corrupt file reported, not recorded", len(r.errors) == 1))
        r = sync.import_folder(conn, share)
        checks.append(("re-import adds nothing, retries the corrupt file",
                       r.imported == 0 and r.new_files == 1 and len(r.errors) == 1))
        bad.unlink()

        r = store.import_file(conn, pc_a)
        checks.append(("importing PC-A's game file too creates no duplicates", r.imported == 0 and r.skipped == n_a))

        # Folder import must give exactly the same data as importing the game files directly.
        direct = store.connect(tmp / "direct.sqlite")
        store.import_file(direct, pc_a)
        store.import_file(direct, pc_b)
        same_prices = table(conn, "SELECT * FROM prices") == table(direct, "SELECT * FROM prices")
        same_items = table(conn, "SELECT key, item_id, suffix, name, quality FROM items") == \
            table(direct, "SELECT key, item_id, suffix, name, quality FROM items")
        checks.append(("folder import == direct import (prices)", same_prices))
        checks.append(("folder import == direct import (item names)", same_items))
        checks.append(("source records which PC sent it",
                       conn.execute("SELECT COUNT(*) FROM snapshots WHERE source LIKE 'PC-B:%'").fetchone()[0]
                       == n_b - overlap))
        size = max(f.stat().st_size for f in files)
        print(f"PC-A {n_a} snapshots, PC-B {n_b}, {overlap} identical; largest shared file {size / 1024:.1f} KB")
        conn.close()
        direct.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for name, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {name}")
    if not all(ok for _, ok in checks):
        raise SystemExit("sync test failed")


if __name__ == "__main__":
    main()
