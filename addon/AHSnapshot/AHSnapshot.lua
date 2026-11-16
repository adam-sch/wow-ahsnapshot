-- AHSnapshot
-- Records a compact price snapshot of the whole auction house every time a
-- full ("replicate") scan happens while you're at the AH. It starts the scan
-- itself when the 15-minute server cooldown allows, and it also records scans
-- started by other addons (e.g. Auctionator's autoscan), since every addon
-- reads the same replicate data.
--
-- Data lives in the SavedVariable AHSnapshotDB, which WoW writes to
--   WTF\Account\<ACCOUNT>\SavedVariables\AHSnapshot.lua
-- on logout or /reload. The analyzer imports that file.
--
-- AHSnapshotDB = {
--   version   = 1,
--   settings  = { autoScan = true, maxSnapshots = 150 },
--   lastReplicate = <epoch of our last ReplicateItems() call>,
--   items     = { [itemKey] = { n = "Name", q = quality } },
--   snapshots = {
--     { t = <server epoch>, tz = <local UTC offset secs>, realm = "...", faction = "...",
--       auctions = N, noBuyout = N, seconds = <processing time>,
--       items = { [itemKey] = "auctions,quantity,min,p10,p25,p50,max" } },  -- prices = copper per unit
--   },
-- }
-- itemKey is "itemID", or "itemID:suffixID" for random-suffix gear ("of the Bear").

local ADDON_NAME = ...

local DB_VERSION         = 1
local REPLICATE_COOLDOWN = 15 * 60 -- server allows one full scan per account per 15 minutes
local AUTO_SCAN_DELAY    = 2       -- seconds after opening the AH (lets other addons go first)
local BATCH_SIZE         = 500     -- auctions read per frame
local SUMMARY_BATCH      = 300     -- items summarized per frame
local RETRY_INTERVAL     = 0.5     -- seconds between retries for items whose data isn't cached yet
local ITEM_WAIT          = 30      -- seconds to wait for item info, then record the rest by item ID only
local REREQUEST_EVERY    = 4       -- re-ask the server for item data every N retries
local REQUEST_TIMEOUT    = 90
local SCAN_TIMEOUT       = 180     -- give up on a snapshot that hasn't finished in this many seconds
local DUPLICATE_WINDOW   = 10 * 60 -- ignore repeated replicate events for the same scan
local MAX_MISSING_RATIO  = 0.05    -- discard the snapshot if more than 5% of auctions were unreadable

local DEFAULTS = { autoScan = true, maxSnapshots = 150 }

local db
local ahOpen = false
local scan                -- state of the snapshot being processed, or nil
local requestedAt         -- GetTime() of our pending ReplicateItems() call
local lastProcessedAt     -- GetTime() when we last started processing replicate data
local button

local floor, ceil, max, format = math.floor, math.ceil, math.max, string.format

local function Print(msg)
  print("|cff33ff99AHSnapshot|r: " .. msg)
end

local function Round(x)
  return floor(x + 0.5)
end

local function FormatDuration(seconds)
  seconds = max(0, floor(seconds))
  return format("%d:%02d", floor(seconds / 60), seconds % 60)
end

local UpdateButton -- defined with the UI below

-- Every scan step runs through Run(): if anything errors, the scan is abandoned
-- and the error is printed in chat (WoW hides Lua errors by default), instead of
-- leaving the scan stuck forever.
local function AbortScan(reason)
  scan = nil
  requestedAt = nil
  Print("|cffff5555" .. reason .. "|r")
  UpdateButton()
end

local function Run(fn)
  local ok, err = xpcall(fn, function(e) return tostring(e) end)
  if not ok then
    AbortScan("Error while recording; snapshot abandoned. Please report: " .. err)
  end
end

local function After(delay, fn)
  C_Timer.After(delay, function() Run(fn) end)
end

---------------------------------------------------------------------------
-- Saved data
---------------------------------------------------------------------------

local function InitDB()
  AHSnapshotDB = AHSnapshotDB or {}
  db = AHSnapshotDB
  db.version = db.version or DB_VERSION
  db.settings = db.settings or {}
  for k, v in pairs(DEFAULTS) do
    if db.settings[k] == nil then db.settings[k] = v end
  end
  db.items = db.items or {}
  db.snapshots = db.snapshots or {}
end

-- The cooldown is per account and shared by every addon, so also honour
-- Auctionator's record of its own last full scan if it's installed.
local function LastReplicateTime()
  local last = db.lastReplicate or 0
  local other = Auctionator and Auctionator.SavedState and Auctionator.SavedState.TimeOfLastReplicateScan
  if type(other) == "number" and other > last then last = other end
  return last
end

local function SecondsUntilReady()
  return max(0, LastReplicateTime() + REPLICATE_COOLDOWN - time())
end

local function LocalUTCOffset(t)
  local loc = date("*t", t)
  local utc = date("!*t", t)
  utc.isdst = loc.isdst
  return floor(difftime(time(loc), time(utc)))
end

---------------------------------------------------------------------------
-- Item keys
---------------------------------------------------------------------------

-- item:itemID:enchantID:gem1:gem2:gem3:gem4:suffixID:...
local function ItemKey(itemID, link)
  local itemString = link and (link:match("|Hitem:([^|]+)|h") or link:match("^item:(.+)"))
  if itemString then
    local fields, n = {}, 0
    for field in (itemString .. ":"):gmatch("([^:]*):") do
      n = n + 1
      fields[n] = field
      if n >= 7 then break end
    end
    local suffix = tonumber(fields[7])
    if suffix and suffix ~= 0 then
      return itemID .. ":" .. suffix
    end
  end
  return tostring(itemID)
end

---------------------------------------------------------------------------
-- Snapshot processing
---------------------------------------------------------------------------

local function Record(info, link)
  local name, count, quality, buyout, itemID = info[1], info[3], info[4], info[10], info[17]
  local key = ItemKey(itemID, link)

  if not db.items[key] then
    local linkName = link and link:match("|h%[(.-)%]|h")
    local itemName = linkName or name
    if itemName and itemName ~= "" then
      db.items[key] = { n = itemName, q = quality }
    end
  end

  scan.auctions = scan.auctions + 1
  if not count or count <= 0 or not buyout or buyout <= 0 then
    scan.noBuyout = scan.noBuyout + 1 -- bid-only auction: no buyout price to record
    return
  end

  local bucket = scan.buckets[key]
  if not bucket then
    bucket = {}
    scan.buckets[key] = bucket
    scan.keys[#scan.keys + 1] = key
  end
  -- flat pairs: unit price, quantity
  bucket[#bucket + 1] = buyout / count
  bucket[#bucket + 1] = count
end

local PERCENTILES = { 0.10, 0.25, 0.50 }

-- Quantity-weighted stats for one item, encoded as
-- "auctions,quantity,min,p10,p25,p50,max" (copper per unit).
local function Summarize(bucket)
  local n = #bucket / 2
  local order = {}
  local totalQty = 0
  for i = 1, n do
    order[i] = i
    totalQty = totalQty + bucket[2 * i]
  end
  table.sort(order, function(a, b) return bucket[2 * a - 1] < bucket[2 * b - 1] end)

  local result = {}
  local cumulative, t = 0, 1
  local needed = max(1, ceil(PERCENTILES[t] * totalQty))
  for _, i in ipairs(order) do
    cumulative = cumulative + bucket[2 * i]
    while t <= #PERCENTILES and cumulative >= needed do
      result[t] = bucket[2 * i - 1]
      t = t + 1
      if t <= #PERCENTILES then needed = max(1, ceil(PERCENTILES[t] * totalQty)) end
    end
  end

  -- %.0f, not %d: unit prices can exceed 2^31 copper (troll listings up to ~10M gold),
  -- which overflows %d.
  return format("%d,%d,%.0f,%.0f,%.0f,%.0f,%.0f",
    n, totalQty,
    Round(bucket[2 * order[1] - 1]),
    Round(result[1]), Round(result[2]), Round(result[3]),
    Round(bucket[2 * order[n] - 1]))
end

local function ScanProgressText(state)
  if state.phase == "reading" then
    return format("reading auctions %d%%", floor(state.nextIndex / state.total * 100))
  elseif state.phase == "loading" then
    return format("waiting for item info on %d auctions", #state.pending)
  end
  return "saving"
end

function UpdateButton()
  if not button then return end
  if scan then
    if scan.phase == "reading" then
      button:SetText(format("Recording %d%%", floor(scan.nextIndex / scan.total * 100)))
    elseif scan.phase == "loading" then
      button:SetText("Loading items...")
    else
      button:SetText("Saving...")
    end
    button:Disable()
  elseif requestedAt then
    button:SetText("Waiting...")
    button:Disable()
  else
    button:SetText("Snapshot")
    button:Enable()
  end
end

local function Finish(snapshot, message)
  scan = nil
  if snapshot then
    table.insert(db.snapshots, snapshot)
    local limit = tonumber(db.settings.maxSnapshots) or DEFAULTS.maxSnapshots
    local dropped = 0
    while #db.snapshots > limit do
      table.remove(db.snapshots, 1)
      dropped = dropped + 1
    end
    if dropped > 0 then
      message = message .. format(" (dropped %d oldest; limit %d, change with /ahs max <n>)", dropped, limit)
    end
  end
  Print(message)
  UpdateButton()
end

local function Finalize(state)
  if scan ~= state then return end

  if state.missing > state.total * MAX_MISSING_RATIO then
    Finish(nil, format("Discarded this snapshot: %d of %d auctions could not be read (was the AH closed mid-scan?).",
      state.missing, state.total))
    return
  end

  state.phase = "saving"
  UpdateButton()
  local out, count, index = {}, 0, 1
  local function Step()
    if scan ~= state then return end
    local last = math.min(index + SUMMARY_BATCH - 1, #state.keys)
    for i = index, last do
      local key = state.keys[i]
      out[key] = Summarize(state.buckets[key])
      count = count + 1
    end
    index = last + 1
    if index <= #state.keys then
      After(0, Step)
      return
    end

    local snapshot = {
      t        = state.serverTime,
      tz       = LocalUTCOffset(state.serverTime),
      realm    = GetRealmName(),
      faction  = UnitFactionGroup("player"),
      auctions = state.auctions,
      noBuyout = state.noBuyout,
      seconds  = Round(GetTime() - state.startedAt),
      items    = out,
    }
    Finish(snapshot, format("Saved snapshot #%d: %d auctions, %d items. Stored on logout or /reload.",
      #db.snapshots + 1, state.auctions, count))
  end
  Step()
end

-- Re-check auctions whose item data wasn't cached yet, a batch per frame.
-- After ITEM_WAIT seconds, record whatever is left using just the item ID
-- (prices are still right; only the name and random suffix may be missing).
local function RetryPending(state, attempt)
  if scan ~= state then return end
  local final = GetTime() - state.loadingSince >= ITEM_WAIT
  local rerequest = attempt % REREQUEST_EVERY == 0
  local pending, stillPending, requested = state.pending, {}, {}
  local i = 1

  local function Step()
    if scan ~= state then return end
    local last = math.min(i + BATCH_SIZE - 1, #pending)
    for j = i, last do
      local index = pending[j]
      local info = { C_AuctionHouse.GetReplicateItemInfo(index) }
      local itemID = info[17]
      if not itemID then
        state.missing = state.missing + 1
      elseif info[18] or final then
        Record(info, C_AuctionHouse.GetReplicateItemLink(index))
      else
        stillPending[#stillPending + 1] = index
        if rerequest and not requested[itemID] then
          requested[itemID] = true
          C_Item.RequestLoadItemDataByID(itemID)
        end
      end
    end
    i = last + 1
    if i <= #pending then
      After(0, Step)
      return
    end

    state.pending = stillPending
    UpdateButton()
    if #stillPending == 0 then
      Finalize(state)
    else
      After(RETRY_INTERVAL, function() RetryPending(state, attempt + 1) end)
    end
  end
  Step()
end

local function ProcessBatch(state)
  if scan ~= state then return end
  local last = math.min(state.nextIndex + BATCH_SIZE, state.total) - 1
  for index = state.nextIndex, last do
    local info = { C_AuctionHouse.GetReplicateItemInfo(index) }
    local itemID = info[17]
    if not itemID then
      state.missing = state.missing + 1
    elseif not C_Item.DoesItemExistByID(itemID) then
      -- Known Blizzard glitch: occasionally returns entries for items that don't exist.
    elseif info[18] then -- hasAllInfo
      Record(info, C_AuctionHouse.GetReplicateItemLink(index))
    else
      state.pending[#state.pending + 1] = index
      if not state.requested[itemID] then
        state.requested[itemID] = true
        C_Item.RequestLoadItemDataByID(itemID)
      end
    end
  end
  state.nextIndex = last + 1
  UpdateButton()

  if state.nextIndex < state.total then
    After(0, function() ProcessBatch(state) end)
  elseif #state.pending > 0 then
    state.phase = "loading"
    state.loadingSince = GetTime()
    UpdateButton()
    Print(format("Waiting for the server to send item info for %d auctions (items not seen before)...",
      #state.pending))
    After(RETRY_INTERVAL, function() RetryPending(state, 1) end)
  else
    Finalize(state)
  end
end

local function OnReplicateData()
  if scan then return end
  if lastProcessedAt and GetTime() - lastProcessedAt < DUPLICATE_WINDOW then return end

  local total = C_AuctionHouse.GetNumReplicateItems() or 0
  requestedAt = nil
  if total == 0 then
    Print("The full scan returned no auctions; nothing recorded.")
    return
  end

  lastProcessedAt = GetTime()
  scan = {
    total      = total,
    nextIndex  = 0, -- replicate indices are 0-based
    buckets    = {},
    keys       = {},
    pending    = {},
    requested  = {},
    auctions   = 0,
    noBuyout   = 0,
    missing    = 0,
    startedAt  = GetTime(),
    serverTime = GetServerTime(),
    phase      = "reading",
  }
  local state = scan
  UpdateButton()
  Print(format("Recording %d auctions...", total))
  After(SCAN_TIMEOUT, function()
    if scan == state then
      AbortScan(format("Snapshot timed out after %d seconds while %s; abandoned.",
        SCAN_TIMEOUT, ScanProgressText(state)))
    end
  end)
  ProcessBatch(state)
end

local function RequestScan(manual)
  if not ahOpen then
    if manual then Print("Open the auction house first.") end
    return
  end
  if scan then
    if manual then Print("Already recording a snapshot.") end
    return
  end
  if requestedAt then
    if manual then Print("Waiting for the server to send scan data...") end
    return
  end

  local wait = SecondsUntilReady()
  if wait > 0 then
    local sinceLast = time() - LastReplicateTime()
    if sinceLast < 60 then
      Print("Another addon just started a full scan; its results will be recorded.")
    else
      Print(format("Next full scan allowed in %s (server limit: one per 15 minutes).", FormatDuration(wait)))
    end
    return
  end

  db.lastReplicate = time()
  requestedAt = GetTime()
  local thisRequest = requestedAt
  UpdateButton()
  Print("Requesting a full auction house scan...")
  C_AuctionHouse.ReplicateItems()

  C_Timer.After(REQUEST_TIMEOUT, function()
    if requestedAt == thisRequest and not scan then
      requestedAt = nil
      UpdateButton()
      Print("No scan data arrived. The server may be throttling scans; try again in 15 minutes.")
    end
  end)
end

---------------------------------------------------------------------------
-- UI
---------------------------------------------------------------------------

local function ShowTooltip(owner)
  GameTooltip:SetOwner(owner, "ANCHOR_RIGHT")
  GameTooltip:AddLine("AHSnapshot")
  GameTooltip:AddLine(format("%d snapshots stored", #db.snapshots), 1, 1, 1)
  local latest = db.snapshots[#db.snapshots]
  if latest then
    GameTooltip:AddLine(format("Last snapshot: %s ago", FormatDuration(GetServerTime() - latest.t)), 1, 1, 1)
  end
  local wait = SecondsUntilReady()
  if wait > 0 then
    GameTooltip:AddLine(format("Next scan allowed in %s", FormatDuration(wait)), 1, 0.8, 0.2)
  else
    GameTooltip:AddLine("Click to scan now", 0.2, 1, 0.2)
  end
  GameTooltip:Show()
end

local function EnsureButton()
  if button or not AuctionHouseFrame then return end
  button = CreateFrame("Button", "AHSnapshotButton", AuctionHouseFrame, "UIPanelButtonTemplate")
  button:SetSize(110, 22)
  button:SetPoint("TOPRIGHT", AuctionHouseFrame, "BOTTOMRIGHT", 0, -2)
  button:SetScript("OnClick", function() RequestScan(true) end)
  button:SetScript("OnEnter", ShowTooltip)
  button:SetScript("OnLeave", function() GameTooltip:Hide() end)
  UpdateButton()
end

---------------------------------------------------------------------------
-- Events
---------------------------------------------------------------------------

local frame = CreateFrame("Frame")
frame:RegisterEvent("ADDON_LOADED")
frame:RegisterEvent("AUCTION_HOUSE_SHOW")
frame:RegisterEvent("AUCTION_HOUSE_CLOSED")
frame:RegisterEvent("REPLICATE_ITEM_LIST_UPDATE")

frame:SetScript("OnEvent", function(_, event, arg1)
  if event == "ADDON_LOADED" then
    if arg1 == ADDON_NAME then
      InitDB()
      frame:UnregisterEvent("ADDON_LOADED")
    end
  elseif not db then
    return
  elseif event == "AUCTION_HOUSE_SHOW" then
    ahOpen = true
    EnsureButton()
    if db.settings.autoScan then
      C_Timer.After(AUTO_SCAN_DELAY, function()
        if ahOpen then RequestScan(false) end
      end)
    end
  elseif event == "AUCTION_HOUSE_CLOSED" then
    ahOpen = false
    if scan then
      Print("Auction house closed while recording; finishing with what is still readable.")
    end
  elseif event == "REPLICATE_ITEM_LIST_UPDATE" then
    Run(OnReplicateData)
  end
end)

---------------------------------------------------------------------------
-- Slash commands
---------------------------------------------------------------------------

local function ShowStatus()
  if scan then
    Print(format("Recording now: %s (%d auctions, %ds so far).",
      ScanProgressText(scan), scan.total, floor(GetTime() - scan.startedAt)))
  elseif requestedAt then
    Print(format("Waiting for the server to send scan data (%ds so far).", floor(GetTime() - requestedAt)))
  end
  Print(format("%d snapshots stored (limit %d). Auto-scan is %s.",
    #db.snapshots, db.settings.maxSnapshots, db.settings.autoScan and "on" or "off"))
  local latest = db.snapshots[#db.snapshots]
  if latest then
    Print(format("Last snapshot %s ago: %d auctions on %s.",
      FormatDuration(GetServerTime() - latest.t), latest.auctions or 0, latest.realm or "?"))
  end
  local wait = SecondsUntilReady()
  Print(wait > 0 and format("Next full scan allowed in %s.", FormatDuration(wait)) or "A full scan is allowed now.")
end

SLASH_AHSNAPSHOT1 = "/ahs"
SLASH_AHSNAPSHOT2 = "/ahsnapshot"
SlashCmdList.AHSNAPSHOT = function(input)
  if not db then return end
  local cmd, rest = (input or ""):lower():match("^%s*(%S*)%s*(.-)%s*$")
  if cmd == "scan" then
    RequestScan(true)
  elseif cmd == "status" or cmd == "" then
    ShowStatus()
  elseif cmd == "auto" then
    if rest == "on" then db.settings.autoScan = true
    elseif rest == "off" then db.settings.autoScan = false
    else db.settings.autoScan = not db.settings.autoScan end
    Print("Auto-scan on AH open is " .. (db.settings.autoScan and "on." or "off."))
  elseif cmd == "max" then
    local n = tonumber(rest)
    if n and n >= 1 then
      db.settings.maxSnapshots = floor(n)
      Print(format("Will keep at most %d snapshots (oldest are dropped first).", db.settings.maxSnapshots))
    else
      Print("Usage: /ahs max <number>")
    end
  elseif cmd == "cancel" then
    if scan or requestedAt then
      AbortScan("Snapshot cancelled.")
    else
      Print("Nothing to cancel.")
    end
  elseif cmd == "clear" then
    if rest == "yes" then
      local n = #db.snapshots
      db.snapshots = {}
      Print(format("Deleted %d stored snapshots. Import them with the analyzer first!", n))
    else
      Print(format("This deletes all %d stored snapshots. Type /ahs clear yes to confirm.", #db.snapshots))
    end
  else
    Print("Commands: /ahs status | scan | cancel | auto [on|off] | max <n> | clear")
  end
end
