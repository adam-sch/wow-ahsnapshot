# AHSnapshot

WoW: Forever auction house snapshot addon (`addon/AHSnapshot`, Lua 5.1) plus a Python price analyzer (`analyzer/`, stdlib only). See README.md for usage.

## Git commits and pull requests

- Commits are authored by adam-sch using the normal git identity. Never add a co-author.
- Never add Claude/AI attribution anywhere: no "Co-Authored-By: Claude ..." trailers in commit messages, and no "Generated with Claude Code" (or similar) lines in PR descriptions, PR or issue comments, or release notes.
- Leave git's commit-signing settings as they are (don't add --no-gpg-sign or -S).

## Project notes

- Game client: WoW: Forever, installed at `C:\Program Files (x86)\World of Warcraft\_forever_`. TOC interface `16001`; uses the modern `C_AuctionHouse` replicate (full-scan) API.
- The server allows one full scan per account every 15 minutes. Auctionator is installed too and auto-scans on AH open; the addon records those scans instead of competing for the cooldown.
- Addon test (no game needed): `python tests/addon_sim.py` runs the real addon under LuaJIT with a mocked WoW API (needs `pip install lupa`).
- Analyzer: `python analyzer/ahs.py -h`.
