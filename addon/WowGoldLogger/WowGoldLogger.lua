-- WowGold Logger: a session recorder for the wowgold planner.
--
-- Nothing is recorded until you start a session. While one runs, the addon writes a raw,
-- timestamped event log (kills, loot windows, items received, coins, combat, casts, position,
-- XP, deaths, money); the planner reads it and works out kill speed, respawn times,
-- competition, drop rates and gold per hour.
--
--   /wgl start [note]   start recording (the note is free text, e.g. "gryphons hillsbrad")
--   /wgl pause | resume
--   /wgl stop           finish the session, then /reload so WoW writes it to disk
--   /wgl status         what's being recorded
--   /wgl                open/close the session panel (also the minimap button)
--   /wgl minimap        hide/show the minimap button
--
-- Only reads game events; it never automates anything.

local Logger = {}
WowGoldLogger = Logger -- exposed for debugging and tests

local VERSION = 3
local PREFIX = "|cffe6c200WowGold|r "
local POS_INTERVAL = 5 -- seconds between position samples (only when you've moved)
local STALE_AFTER = 30 * 60 -- a session untouched this long when you log in is finished
local MAX_EVENTS = 200000 -- per session (~a long evening); recording stops beyond it
local MAX_RECORDINGS = 200

Logger.VERSION = VERSION
Logger.STALE_AFTER = STALE_AFTER

local function say(msg)
    DEFAULT_CHAT_FRAME:AddMessage(PREFIX .. msg)
end

-- Turn a GlobalStrings format ("You receive loot: %sx%d.") into an anchored Lua pattern.
local function toPattern(fmt)
    local p = fmt:gsub("[%(%)%.%+%-%*%?%[%]%^%$]", "%%%0")
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

-- "Creature-0-4372-0-17-7105-0000ABCDEF" -> "Creature", 7105, "0000ABCDEF"
function Logger.parseGuid(guid)
    if type(guid) ~= "string" then return nil end
    local kind, id, uid = guid:match("^(%a+)%-%d+%-%d+%-%d+%-%d+%-(%d+)%-(%x+)$")
    if not kind then return nil end
    return kind, tonumber(id), uid
end

local db -- WowGoldLoggerDB, set on ADDON_LOADED
local rec -- the session being recorded (db.current), or nil

local function now() return time() end

-- Event log -----------------------------------------------------------------------------
-- Each event is one string: "<seconds since start>|<code>|<fields...>". Codes:
--   Z zone|subzone|mapId     P mapId|x|y (percent)     C 1|0 combat start/end
--   K npcId|spawnUid|x|y (you or your pet killed it)   D npcId|spawnUid (died near you)
--   L kind|id|spawnUid|slots (loot window opened)      S slot|itemId|qty|kind|id|spawnUid
--   I itemId|count (item received)    M copper (coins looted)    $ copper (money total)
--   A spellId (your cast)    X xp gained    V level    T skill text    R dead|alive
--   Q questId|xp|copper      U pause|resume|stop

local function clean(v)
    if v == nil then return "" end
    return (tostring(v):gsub("|", "/"))
end

-- Running totals for the in-game panel (also rebuilt from the log, see Logger.summary).
local function tally(sum, code, a, b)
    if code == "K" then
        sum.kills = sum.kills + 1
    elseif code == "M" then
        sum.copper = sum.copper + (tonumber(a) or 0)
    elseif code == "I" then
        local id = tonumber(a)
        if id then sum.items[id] = (sum.items[id] or 0) + (tonumber(b) or 1) end
    end
end

local function newSum() return { kills = 0, copper = 0, items = {} } end

local function notify()
    if Logger.onChange then Logger.onChange() end
end

local function log(code, ...)
    if not rec or rec.paused then return end
    if #rec.events >= MAX_EVENTS then
        if not rec.full then
            rec.full = true
            say("session is full (" .. MAX_EVENTS .. " events): /wgl stop and start a new one")
        end
        return
    end
    local parts = { tostring(now() - rec.start), code }
    for i = 1, select("#", ...) do parts[#parts + 1] = clean((select(i, ...))) end
    rec.events[#rec.events + 1] = table.concat(parts, "|")
    rec.last = now()
    if code == "K" or code == "M" or code == "I" then
        rec.sum = rec.sum or newSum()
        tally(rec.sum, code, ...)
        notify()
    end
end
Logger.log = log

-- Totals of a session (the current one or a saved one): kills, coins looted, items received.
function Logger.summary(r)
    if not r then return nil end
    if not r.sum then
        local sum = newSum()
        for _, e in ipairs(r.events or {}) do
            local _, code, a, b = e:match("^(%d+)|(.)|([^|]*)|?([^|]*)")
            if code then tally(sum, code, a, b) end
        end
        r.sum = sum
    end
    return r.sum
end

local function round(x, digits)
    local m = 10 ^ (digits or 0)
    return math.floor(x * m + 0.5) / m
end

local lastPos = {}

local function position()
    local mapId = C_Map and C_Map.GetBestMapForUnit and C_Map.GetBestMapForUnit("player")
    if not mapId or not C_Map.GetPlayerMapPosition then return mapId end
    local pos = C_Map.GetPlayerMapPosition(mapId, "player")
    if not pos then return mapId end
    local x, y
    if pos.GetXY then x, y = pos:GetXY() else x, y = pos.x, pos.y end
    if not x or not y then return mapId end
    return mapId, round(x * 100, 2), round(y * 100, 2)
end
Logger.position = position

local function logPosition(force)
    local mapId, x, y = position()
    if not x then return end
    if not force and lastPos.map == mapId and lastPos.x == x and lastPos.y == y then return end
    lastPos.map, lastPos.x, lastPos.y = mapId, x, y
    log("P", mapId, x, y)
end

local function logZone()
    local mapId = position()
    log("Z", GetZoneText and GetZoneText() or "", GetSubZoneText and GetSubZoneText() or "", mapId)
end

-- Session header ------------------------------------------------------------------------

local function professions()
    local out = {}
    if not GetNumSkillLines then return out end
    for i = 1, GetNumSkillLines() do
        local name, isHeader, _, rank, _, _, maxRank = GetSkillLineInfo(i)
        if name and not isHeader then
            out[#out + 1] = { name = name, rank = rank, max = maxRank }
        end
    end
    return out
end

local function header()
    local _, class = UnitClass("player")
    local race
    if UnitRace then _, race = UnitRace("player") end
    return {
        name = UnitName and UnitName("player"),
        realm = GetRealmName and GetRealmName(),
        class = class,
        race = race,
        level = UnitLevel("player"),
        faction = UnitFactionGroup and UnitFactionGroup("player"),
        skills = professions(),
    }
end

-- Commands -------------------------------------------------------------------------------

local lastXp

-- Recorded time of a session, without pauses.
function Logger.elapsed(r)
    r = r or rec
    if not r then return 0 end
    local paused = r.pausedFor or 0
    if r.paused then paused = paused + (now() - r.paused) end
    return math.max(0, (r.stop or now()) - r.start - paused)
end
local function elapsed() return Logger.elapsed(rec) end

function Logger.current() return rec end
function Logger.lastRecording() return db and db.recordings[#db.recordings] end
function Logger.db() return db end

function Logger.start(note)
    if rec then return say("already recording (/wgl stop first)") end
    local t = now()
    rec = {
        id = string.format("%d-%04d", t, math.random(0, 9999)),
        v = VERSION,
        note = note ~= "" and note or nil,
        start = t,
        last = t,
        char = header(),
        events = {},
        kills = 0,
        pausedFor = 0,
        sum = newSum(),
    }
    db.current = rec
    lastPos = {}
    lastXp = UnitXP and UnitXP("player")
    logZone()
    logPosition(true)
    if GetMoney then log("$", GetMoney()) end
    say("recording" .. (rec.note and (" \"" .. rec.note .. "\"") or "") .. " (/wgl stop when done)")
    notify()
end

function Logger.stop(reason)
    if not rec then return say("not recording") end
    if rec.paused then Logger.resume(true) end
    if reason == "stale" then
        -- found on login: the session really ended with its last event
        rec.stop = rec.last or rec.start
        rec.ended = "stale"
    else
        if GetMoney then log("$", GetMoney()) end
        log("U", reason or "stop")
        rec.stop = now()
    end
    table.insert(db.recordings, rec)
    while #db.recordings > MAX_RECORDINGS do table.remove(db.recordings, 1) end
    db.current = nil
    local mins = elapsed() / 60
    rec = nil
    notify()
    say(string.format("session saved (%d min). Type /reload to write it to disk for the planner.", mins))
end

function Logger.pause()
    if not rec then return say("not recording") end
    if rec.paused then return say("already paused") end
    log("U", "pause")
    rec.paused = now()
    say("paused (/wgl resume)")
    notify()
end

function Logger.resume(quiet)
    if not rec then return say("not recording") end
    if not rec.paused then return say("not paused") end
    rec.pausedFor = (rec.pausedFor or 0) + (now() - rec.paused)
    rec.paused = nil
    log("U", "resume")
    logZone()
    logPosition(true)
    if not quiet then say("recording again") end
    notify()
end

function Logger.status()
    if not rec then
        return say(string.format("not recording; %d saved session(s). /wgl start [note]", #db.recordings))
    end
    say(string.format("%s%s: %d min, %d kills, %d events%s", rec.paused and "paused" or "recording",
        rec.note and (" \"" .. rec.note .. "\"") or "", elapsed() / 60, rec.kills or 0, #rec.events,
        rec.full and " (full)" or ""))
end

-- Events --------------------------------------------------------------------------------

local function onCombatLog()
    local _, subevent, _, sourceGuid, _, _, _, destGuid = CombatLogGetCurrentEventInfo()
    if subevent ~= "PARTY_KILL" and subevent ~= "UNIT_DIED" then return end
    local kind, id, uid = Logger.parseGuid(destGuid)
    if kind ~= "Creature" and kind ~= "Vehicle" then return end
    if subevent == "PARTY_KILL" then
        if sourceGuid ~= UnitGUID("player") and sourceGuid ~= UnitGUID("pet") then return end
        local _, x, y = position()
        log("K", id, uid, x, y)
        rec.kills = (rec.kills or 0) + 1
        notify()
    else
        log("D", id, uid)
    end
end

local function onLootOpened()
    local n = GetNumLootItems and GetNumLootItems() or 0
    local kind, id, uid = Logger.parseGuid(GetLootSourceInfo and n > 0 and (GetLootSourceInfo(1)) or nil)
    log("L", kind, id, uid, n)
    for slot = 1, n do
        local link = GetLootSlotLink and GetLootSlotLink(slot)
        local itemId = link and link:match("item:(%d+)")
        local qty = 0
        if GetLootSlotInfo then qty = select(3, GetLootSlotInfo(slot)) or 0 end
        local sk, sid, suid = Logger.parseGuid(GetLootSourceInfo and (GetLootSourceInfo(slot)) or nil)
        log("S", slot, itemId, qty, sk, sid, suid)
    end
end

function Logger.onEvent(event, ...)
    local arg1 = ...
    if event == "ADDON_LOADED" and arg1 == "WowGoldLogger" then
        WowGoldLoggerDB = WowGoldLoggerDB or {}
        db = WowGoldLoggerDB
        db.recordings = db.recordings or {}
        db.version = VERSION
        -- v0.2 kept recording automatically into db.segments/db.open; that's gone now.
        db.open = nil
        rec = db.current
        if rec and now() - (rec.last or rec.start) > STALE_AFTER then
            -- logged out (or crashed) mid-session a while ago: keep what's there
            rec.paused = nil
            Logger.stop("stale")
        elseif rec then
            say("still recording" .. (rec.note and (" \"" .. rec.note .. "\"") or "") .. " (/wgl stop when done)")
        end
        notify()
        return
    end
    if not rec or rec.paused then return end
    if event == "COMBAT_LOG_EVENT_UNFILTERED" then
        onCombatLog()
    elseif event == "LOOT_OPENED" then
        onLootOpened()
    elseif event == "CHAT_MSG_LOOT" then
        local itemId, count = Logger.parseLoot(arg1)
        if itemId then log("I", itemId, count) end
    elseif event == "CHAT_MSG_MONEY" then
        local copper = Logger.parseMoney(arg1)
        if copper > 0 then log("M", copper) end
    elseif event == "PLAYER_MONEY" then
        if GetMoney then log("$", GetMoney()) end
    elseif event == "UNIT_SPELLCAST_SUCCEEDED" then
        local unit, _, spellId = ...
        if unit == "player" then log("A", spellId) end
    elseif event == "PLAYER_REGEN_DISABLED" then
        log("C", 1)
    elseif event == "PLAYER_REGEN_ENABLED" then
        log("C", 0)
    elseif event == "ZONE_CHANGED_NEW_AREA" or event == "ZONE_CHANGED" then
        logZone()
        logPosition(true)
    elseif event == "PLAYER_XP_UPDATE" then
        local xp = UnitXP and UnitXP("player")
        if xp and lastXp and xp > lastXp then log("X", xp - lastXp) end
        lastXp = xp
    elseif event == "PLAYER_LEVEL_UP" then
        log("V", arg1)
        lastXp = 0
    elseif event == "CHAT_MSG_SKILL" then
        log("T", arg1)
    elseif event == "PLAYER_DEAD" then
        log("R", "dead")
    elseif event == "PLAYER_ALIVE" or event == "PLAYER_UNGHOST" then
        log("R", "alive")
    elseif event == "QUEST_TURNED_IN" then
        local questId, xp, money = ...
        log("Q", questId, xp, money)
    end
end

-- Position sampling: an OnUpdate timer, POS_INTERVAL seconds apart.
local sinceSample = 0
function Logger.onUpdate(elapsedSecs)
    if not rec or rec.paused then return end
    sinceSample = sinceSample + elapsedSecs
    if sinceSample >= POS_INTERVAL then
        sinceSample = 0
        logPosition(false)
        notify()
    end
end

local events = CreateFrame("Frame")
for _, e in ipairs({
    "ADDON_LOADED", "COMBAT_LOG_EVENT_UNFILTERED", "LOOT_OPENED", "CHAT_MSG_LOOT",
    "CHAT_MSG_MONEY", "PLAYER_MONEY", "UNIT_SPELLCAST_SUCCEEDED", "PLAYER_REGEN_DISABLED",
    "PLAYER_REGEN_ENABLED", "ZONE_CHANGED_NEW_AREA", "ZONE_CHANGED", "PLAYER_XP_UPDATE",
    "PLAYER_LEVEL_UP", "CHAT_MSG_SKILL", "PLAYER_DEAD", "PLAYER_ALIVE", "PLAYER_UNGHOST",
    "QUEST_TURNED_IN",
}) do
    events:RegisterEvent(e)
end
events:SetScript("OnEvent", function(_, event, ...) Logger.onEvent(event, ...) end)
events:SetScript("OnUpdate", function(_, e) Logger.onUpdate(e) end)

SLASH_WOWGOLDLOGGER1 = "/wgl"
SlashCmdList["WOWGOLDLOGGER"] = function(input)
    local cmd, rest = (input or ""):match("^%s*(%S*)%s*(.-)%s*$")
    cmd = (cmd or ""):lower()
    if cmd == "start" then Logger.start(rest or "")
    elseif cmd == "stop" then Logger.stop()
    elseif cmd == "pause" then Logger.pause()
    elseif cmd == "resume" then Logger.resume()
    elseif cmd == "status" then Logger.status()
    elseif cmd == "" or cmd == "show" or cmd == "hide" or cmd == "toggle" then
        if Logger.ui then Logger.ui.panel(cmd == "" and "toggle" or cmd)
        else say("/wgl start [note] | pause | resume | stop | status") end
    elseif cmd == "minimap" then
        if Logger.ui then Logger.ui.minimap() end
    else
        say("/wgl (panel) | start [note] | pause | resume | stop | status | minimap (button on/off)")
    end
end
