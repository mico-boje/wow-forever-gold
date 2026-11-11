-- WowGold Logger: records what you loot, where it came from and what you killed, so the
-- wowgold planner can work out which farm you were doing and measure its real yields.
--
-- Recording is automatic. Activity is grouped into segments: a new segment starts when you
-- change zone or after IDLE_GAP seconds without loot or kills. The planner matches each
-- segment to a method on import (`wowgold session import`).
--
--   /wgl status              current segment
--   /wgl list                last finished segments
--   /wgl pause | resume      stop/continue recording entirely
--   /wgl start <method_id>   optional: label what you're doing (skips auto-matching)
--   /wgl stop                end the label (and the current segment)
--
-- Only reads loot/money/combat-log events; it never automates anything.

local Logger = {}
WowGoldLogger = Logger -- exposed for debugging and tests

local PREFIX = "|cffe6c200WowGold|r "
local IDLE_GAP = 10 * 60 -- seconds without activity that end a segment
local ACTIVE_CAP = 5 * 60 -- longest gap between two events still counted as active time
local LOOT_LINGER = 3 -- seconds after a loot window closes that loot is still attributed
local SPELL_WINDOW = 10 -- a gathering cast this recent explains the next loot window
local MAX_SEGMENTS = 1000

Logger.IDLE_GAP = IDLE_GAP

local function say(msg)
    DEFAULT_CHAT_FRAME:AddMessage(PREFIX .. msg)
end

-- Turn a GlobalStrings format ("You receive loot: %sx%d.") into an anchored Lua pattern.
local function toPattern(fmt)
    -- Escape magic characters (but not %, which only appears in placeholders).
    local p = fmt:gsub("[%(%)%.%+%-%*%?%[%]%^%$]", "%%%0")
    -- Swap placeholders for marker bytes first, so captures inserted for one placeholder
    -- (which contain "%d") aren't rewritten by the next substitution.
    -- Positional forms used by some locales: %1$s (its $ is now escaped as %$).
    p = p:gsub("%%%d%%%$s", "\001"):gsub("%%%d%%%$d", "\002")
    p = p:gsub("%%s", "\001"):gsub("%%d", "\002")
    p = p:gsub("\001", "(.+)"):gsub("\002", "(%%d+)")
    return "^" .. p .. "$"
end
Logger.toPattern = toPattern

local lootMultiple, lootSingle, moneyParts

local function buildPatterns()
    lootMultiple = toPattern(LOOT_ITEM_SELF_MULTIPLE)
    lootSingle = toPattern(LOOT_ITEM_SELF)
    moneyParts = {
        { pattern = GOLD_AMOUNT:gsub("%%d", "(%%d+)"), factor = 10000 },
        { pattern = SILVER_AMOUNT:gsub("%%d", "(%%d+)"), factor = 100 },
        { pattern = COPPER_AMOUNT:gsub("%%d", "(%%d+)"), factor = 1 },
    }
end

-- "You receive loot: |cff...|Hitem:13463::...|h[Dreamfoil]|h|rx3." -> 13463, 3
function Logger.parseLoot(msg)
    if not lootMultiple then buildPatterns() end
    local link, count = msg:match(lootMultiple)
    if not link then
        link = msg:match(lootSingle)
        count = 1
    end
    if not link then return nil end
    local itemId = link:match("item:(%d+)")
    if not itemId then return nil end
    return tonumber(itemId), tonumber(count) or 1
end

-- "You loot 1 Gold, 20 Silver, 5 Copper" -> 12005
function Logger.parseMoney(msg)
    if not moneyParts then buildPatterns() end
    local total = 0
    for _, part in ipairs(moneyParts) do
        local n = msg:match(part.pattern)
        if n then total = total + tonumber(n) * part.factor end
    end
    return total
end

-- "Creature-0-4372-0-17-7105-0000ABCDEF" -> "Creature", 7105
function Logger.parseGuid(guid)
    if type(guid) ~= "string" then return nil end
    local kind, id = guid:match("^(%a+)%-%d+%-%d+%-%d+%-%d+%-(%d+)%-")
    if not kind then return nil end
    return kind, tonumber(id)
end

local db -- WowGoldLoggerDB, set on ADDON_LOADED

-- Loot attribution state (not saved).
local lootSource -- "Creature:7105", "GameObject:181270" or "unknown"
local lootSourceUntil = 0 -- time() until which the last loot window's source still applies
local lastSpell, lastSpellAt = nil, 0

local function now() return time() end

local function zoneInfo()
    local mapId = C_Map and C_Map.GetBestMapForUnit and C_Map.GetBestMapForUnit("player")
    return GetZoneText(), mapId
end

local function closeSegment(reason)
    local seg = db and db.open
    if not seg then return end
    db.open = nil
    seg.closed = reason
    if seg.active < 60 and not next(seg.sources) then return end -- nothing worth keeping
    table.insert(db.segments, seg)
    while #db.segments > MAX_SEGMENTS do table.remove(db.segments, 1) end
end
Logger.closeSegment = closeSegment

local function newSegment(t)
    local zone, mapId = zoneInfo()
    local _, class = UnitClass("player")
    db.open = {
        id = string.format("%d-%04d", t, math.random(0, 9999)),
        zone = zone,
        map = mapId,
        start = t,
        last = t,
        stop = t,
        active = 0,
        class = class,
        level = UnitLevel("player"),
        label = db.label,
        sources = {},
        kills = {},
        copper = 0,
    }
    return db.open
end

-- Current segment for an activity happening now; rolls over on zone change, idle gap or
-- label change.
local function segment()
    local t = now()
    local seg = db.open
    if seg then
        local zone = GetZoneText()
        if zone ~= seg.zone or t - seg.last > IDLE_GAP or seg.label ~= db.label then
            closeSegment(zone ~= seg.zone and "zone" or (t - seg.last > IDLE_GAP and "idle" or "label"))
            seg = nil
        end
    end
    seg = seg or newSegment(t)
    seg.active = seg.active + math.min(t - seg.last, ACTIVE_CAP)
    seg.last, seg.stop = t, t
    return seg
end

local function recording()
    return db and not db.paused
end

local function source(seg, key)
    local s = seg.sources[key]
    if not s then
        s = { loots = 0, items = {}, copper = 0 }
        seg.sources[key] = s
    end
    return s
end

local function currentSourceKey()
    if lootSource and now() <= lootSourceUntil then return lootSource end
    return "unknown"
end

function Logger.onLootOpened()
    local key = "unknown"
    if GetLootSourceInfo and GetNumLootItems and GetNumLootItems() > 0 then
        local kind, id = Logger.parseGuid((GetLootSourceInfo(1)))
        if kind then key = kind .. ":" .. id end
    end
    lootSource, lootSourceUntil = key, math.huge
    if not recording() then return end
    local s = source(segment(), key)
    s.loots = s.loots + 1
    if lastSpell and now() - lastSpellAt <= SPELL_WINDOW then
        s.via = lastSpell
        lastSpell = nil
    end
end

function Logger.onLootClosed()
    lootSourceUntil = now() + LOOT_LINGER
end

function Logger.onLoot(msg)
    if not recording() then return end
    local itemId, count = Logger.parseLoot(msg)
    if not itemId then return end
    local s = source(segment(), currentSourceKey())
    s.items[itemId] = (s.items[itemId] or 0) + count
end

function Logger.onMoney(msg)
    if not recording() then return end
    local copper = Logger.parseMoney(msg)
    if copper <= 0 then return end
    local seg = segment()
    seg.copper = seg.copper + copper
    local s = source(seg, currentSourceKey())
    s.copper = s.copper + copper
end

function Logger.onKill(sourceGuid, destGuid)
    if not recording() then return end
    if sourceGuid ~= UnitGUID("player") and sourceGuid ~= UnitGUID("pet") then return end
    local kind, id = Logger.parseGuid(destGuid)
    if kind ~= "Creature" then return end
    local seg = segment()
    seg.kills[id] = (seg.kills[id] or 0) + 1
end

function Logger.onSpell(spellId)
    lastSpell, lastSpellAt = spellId, now()
end

-- Commands ----------------------------------------------------------------------------

local function describe(seg)
    local items, kills = 0, 0
    for _, s in pairs(seg.sources) do
        for _, qty in pairs(s.items) do items = items + qty end
    end
    for _, n in pairs(seg.kills) do kills = kills + n end
    return string.format("%s%s: %d min, %d items, %d kills, %s", seg.zone or "?",
        seg.label and (" [" .. seg.label .. "]") or "", seg.active / 60, items, kills,
        GetCoinText and GetCoinText(seg.copper, " ") or (seg.copper .. "c"))
end

function Logger.start(method)
    if not method or method == "" then
        return say("usage: /wgl start <method_id>  (see `wowgold methods list`)")
    end
    db.label = method
    closeSegment("label")
    say("labelling activity as " .. method .. " until /wgl stop")
end

function Logger.stop()
    if not db.label then return say("no label set (recording is automatic)") end
    closeSegment("label")
    say("stopped labelling " .. db.label)
    db.label = nil
end

function Logger.pause()
    if db.paused then return say("already paused") end
    closeSegment("pause")
    db.paused = true
    say("recording paused")
end

function Logger.resume()
    if not db.paused then return say("not paused") end
    db.paused = nil
    say("recording resumed")
end

function Logger.status()
    if db.paused then return say("recording paused (/wgl resume)") end
    if not db.open then return say("no activity in the current segment yet") end
    say("current: " .. describe(db.open))
end

function Logger.list()
    local n = #db.segments
    if n == 0 then return say("no finished segments") end
    for i = math.max(1, n - 4), n do say(i .. ". " .. describe(db.segments[i])) end
end

-- Events --------------------------------------------------------------------------------

function Logger.onEvent(event, ...)
    local arg1 = ...
    if event == "ADDON_LOADED" and arg1 == "WowGoldLogger" then
        WowGoldLoggerDB = WowGoldLoggerDB or {}
        db = WowGoldLoggerDB
        db.segments = db.segments or {}
        db.sessions = db.sessions or {} -- v0.1 format, kept for import
        db.version = 2
        -- Close a segment left open if we were away longer than the idle gap.
        if db.open and now() - db.open.last > IDLE_GAP then closeSegment("idle") end
    elseif not db then
        return
    elseif event == "LOOT_OPENED" then
        Logger.onLootOpened()
    elseif event == "LOOT_CLOSED" then
        Logger.onLootClosed()
    elseif event == "CHAT_MSG_LOOT" then
        Logger.onLoot(arg1)
    elseif event == "CHAT_MSG_MONEY" then
        Logger.onMoney(arg1)
    elseif event == "UNIT_SPELLCAST_SUCCEEDED" then
        local unit, _, spellId = ...
        if unit == "player" then Logger.onSpell(spellId) end
    elseif event == "COMBAT_LOG_EVENT_UNFILTERED" then
        local _, subevent, _, sourceGuid, _, _, _, destGuid = CombatLogGetCurrentEventInfo()
        if subevent == "PARTY_KILL" then Logger.onKill(sourceGuid, destGuid) end
    end
end

local frame = CreateFrame("Frame")
for _, e in ipairs({
    "ADDON_LOADED", "LOOT_OPENED", "LOOT_CLOSED", "CHAT_MSG_LOOT", "CHAT_MSG_MONEY",
    "UNIT_SPELLCAST_SUCCEEDED", "COMBAT_LOG_EVENT_UNFILTERED",
}) do
    frame:RegisterEvent(e)
end
frame:SetScript("OnEvent", function(_, event, ...) Logger.onEvent(event, ...) end)

SLASH_WOWGOLDLOGGER1 = "/wgl"
SlashCmdList["WOWGOLDLOGGER"] = function(input)
    local cmd, rest = (input or ""):match("^%s*(%S*)%s*(.-)%s*$")
    cmd = (cmd or ""):lower()
    if cmd == "start" then Logger.start(rest)
    elseif cmd == "stop" then Logger.stop()
    elseif cmd == "pause" then Logger.pause()
    elseif cmd == "resume" then Logger.resume()
    elseif cmd == "status" then Logger.status()
    elseif cmd == "list" then Logger.list()
    else
        say("recording is automatic. /wgl status | list | pause | resume | start <method> | stop")
    end
end
