"""Run the WowGoldLogger addon (v3 session recorder) in an embedded Lua 5.1 with a stubbed WoW
API, then read its SavedVariables with the Python importer and digest the session."""

from __future__ import annotations

from pathlib import Path

import pytest

lua51 = pytest.importorskip("lupa.lua51")

ADDON_DIR = Path(__file__).parent.parent / "addon" / "WowGoldLogger"
ADDON = ADDON_DIR / "WowGoldLogger.lua"
ADDON_UI = ADDON_DIR / "WowGoldLoggerUI.lua"

WOW_STUBS = r"""
LOOT_ITEM_SELF = "You receive loot: %s."
LOOT_ITEM_SELF_MULTIPLE = "You receive loot: %sx%d."
GOLD_AMOUNT = "%d Gold"
SILVER_AMOUNT = "%d Silver"
COPPER_AMOUNT = "%d Copper"
messages = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(self, m) table.insert(messages, m) end }
clock = 1762850000
zone = "Hillsbrad Foothills"
px, py = 0.40, 0.50
money = 100000
xp = 1000
loot = {}  -- slots: { guid, itemId, qty }
cleu = {}
function time() return clock end
function UnitClass() return "Hunter", "HUNTER" end
function UnitRace() return "Orc", "Orc" end
function UnitName() return "Grunt" end
function GetRealmName() return "Forever" end
function UnitFactionGroup() return "Horde", "Horde" end
function UnitLevel() return 40 end
function UnitXP() return xp end
function GetMoney() return money end
function UnitGUID(unit)
    if unit == "player" then return "Player-1-00000001" end
    if unit == "pet" then return "Pet-0-1-1-1-1-00000002" end
end
function GetZoneText() return zone end
function GetSubZoneText() return "" end
function GetNumSkillLines() return 2 end
function GetSkillLineInfo(i)
    if i == 1 then return "Professions", true end
    return "Skinning", false, false, 200, 0, 0, 225
end
C_Map = {
    GetBestMapForUnit = function() return 1424 end,
    GetPlayerMapPosition = function() return { x = px, y = py } end,
}
function GetNumLootItems() return #loot end
function GetLootSourceInfo(slot) return loot[slot] and loot[slot][1] end
function GetLootSlotLink(slot) return loot[slot] and link(loot[slot][2], "x") end
function GetLootSlotInfo(slot) return "icon", "x", loot[slot] and loot[slot][3] or 0 end
function CombatLogGetCurrentEventInfo() return unpack(cleu) end
SlashCmdList = {}
frames, updater = {}, nil
function CreateFrame()
    local f = { events = {} }
    function f:RegisterEvent(e) self.events[e] = true end
    function f:SetScript(name, fn)
        if name == "OnEvent" then self.onEvent = fn elseif name == "OnUpdate" then updater = fn end
    end
    table.insert(frames, f)
    return f
end
function fire(event, ...)
    for _, f in ipairs(frames) do
        if f.onEvent and f.events[event] then f.onEvent(f, event, ...) end
    end
end
function slash(text) SlashCmdList["WOWGOLDLOGGER"](text) end
function link(id, name)
    return "|cffffffff|Hitem:" .. id .. "::::::::60:::::|h[" .. name .. "]|h|r"
end
function guid(npc, uid) return "Creature-0-4372-0-17-" .. npc .. "-" .. uid end
function combatlog(sub, src, dst)
    cleu = { 0, sub, false, src, "x", 0, 0, dst }
    fire("COMBAT_LOG_EVENT_UNFILTERED")
end
-- Kill a mob (you or someone else), then loot it if it was yours.
function kill(npc, uid, itemId, count, coins)
    local g = guid(npc, uid)
    fire("PLAYER_REGEN_DISABLED")
    combatlog("PARTY_KILL", "Player-1-00000001", g)
    combatlog("UNIT_DIED", nil, g)
    fire("PLAYER_REGEN_ENABLED")
    loot = itemId and { { g, itemId, count } } or {}
    fire("LOOT_OPENED")
    if itemId then
        fire("CHAT_MSG_LOOT", "You receive loot: " .. link(itemId, "x") .. "x" .. count .. ".")
    end
    if coins then fire("CHAT_MSG_MONEY", "You loot " .. coins) end
    fire("LOOT_CLOSED")
    loot = {}
end
function otherKill(npc, uid) combatlog("UNIT_DIED", nil, guid(npc, uid)) end
"""

GRYPHON, BEAR = 2347, 2356
FEATHER = 4589  # Long Elegant Feather


@pytest.fixture
def lua():
    rt = lua51.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(WOW_STUBS)
    rt.execute(ADDON.read_text())
    rt.execute(ADDON_UI.read_text())  # builds frames only on PLAYER_LOGIN
    rt.globals().fire("ADDON_LOADED", "WowGoldLogger")
    return rt


def tick(lua, seconds):
    lua.globals().clock += seconds


def events(lua, which="current"):
    rec = lua.globals().WowGoldLoggerDB[which]
    return [rec.events[i] for i in range(1, len(rec.events) + 1)]


def _serialize(value, indent=0) -> str:
    """Write a Lua value the way the WoW client writes SavedVariables."""
    pad = "\t" * (indent + 1)
    if hasattr(value, "items") and not isinstance(value, (str, bytes)):
        lines = ["{"]
        for k, v in value.items():
            key = f'["{k}"]' if isinstance(k, str) else f"[{k}]"
            lines.append(f"{pad}{key} = {_serialize(v, indent + 1)},")
        lines.append("\t" * indent + "}")
        return "\n".join(lines)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return repr(value)


def save(lua, tmp_path) -> Path:
    path = tmp_path / "WowGoldLogger.lua"
    path.write_text("WowGoldLoggerDB = " + _serialize(lua.globals().WowGoldLoggerDB) + "\n")
    return path


def test_patterns(lua):
    logger = lua.globals().WowGoldLogger
    feather = lua.globals().link(FEATHER, "Feather")
    assert logger.parseLoot(f"You receive loot: {feather}x3.") == (FEATHER, 3)
    assert logger.parseLoot(f"You receive loot: {feather}.") == (FEATHER, 1)
    assert logger.parseLoot("Bob receives loot: [Feather].") is None
    assert logger.parseMoney("You loot 1 Gold, 20 Silver, 5 Copper") == 12005
    assert logger.toPattern("Beute: %1$sx%2$d.") == "^Beute: (.+)x(%d+)%.$"
    assert logger.parseGuid("Creature-0-4372-0-17-7105-0000ABCDEF") == (
        "Creature",
        7105,
        "0000ABCDEF",
    )
    assert logger.parseGuid("Player-4372-0A1B2C3D") is None


def test_nothing_is_recorded_until_a_session_starts(lua):
    g = lua.globals()
    g.kill(GRYPHON, "A1", FEATHER, 1)
    assert g.WowGoldLoggerDB.current is None and len(g.WowGoldLoggerDB.recordings) == 0
    g.slash("start gryphons")
    rec = g.WowGoldLoggerDB.current
    assert rec.note == "gryphons" and rec.char["class"] == "HUNTER"
    assert rec.char.faction == "Horde" and rec.char.skills[1].name == "Skinning"
    assert events(lua)[:3] == ["0|Z|Hillsbrad Foothills||1424", "0|P|1424|40|50", "0|$|100000"]


def test_session_log(lua):
    g = lua.globals()
    g.slash("start")
    tick(lua, 10)
    g.kill(GRYPHON, "A1", FEATHER, 2, "1 Silver, 5 Copper")
    log = events(lua)
    assert "10|C|1" in log and "10|C|0" in log
    assert "10|K|2347|A1|40|50" in log and "10|D|2347|A1" in log
    assert "10|L|Creature|2347|A1|1" in log
    assert f"10|S|1|{FEATHER}|2|Creature|2347|A1" in log
    assert f"10|I|{FEATHER}|2" in log and "10|M|105" in log
    # position only when you've moved, every 5 seconds
    lua.globals().updater(None, 5)
    assert sum(e.endswith("|P|1424|40|50") for e in events(lua)) == 1
    g.px = 0.41
    tick(lua, 5)
    lua.globals().updater(None, 5)
    assert events(lua)[-1] == "15|P|1424|41|50"
    # pausing stops recording
    g.slash("pause")
    g.kill(GRYPHON, "A2", FEATHER, 1)
    assert not any("|A2" in e for e in events(lua))
    g.slash("resume")
    g.slash("stop")
    assert g.WowGoldLoggerDB.current is None and len(g.WowGoldLoggerDB.recordings) == 1
    assert "/reload" in g.messages[len(g.messages)]


def test_reload_keeps_recording_and_stale_sessions_finish(lua):
    g = lua.globals()
    g.slash("start")
    g.kill(GRYPHON, "A1", FEATHER, 1)
    tick(lua, 60)
    g.fire("ADDON_LOADED", "WowGoldLogger")  # /reload a minute later: still recording
    assert g.WowGoldLoggerDB.current is not None
    tick(lua, g.WowGoldLogger.STALE_AFTER + 1)
    g.fire("ADDON_LOADED", "WowGoldLogger")  # logged back in much later
    assert g.WowGoldLoggerDB.current is None
    rec = g.WowGoldLoggerDB.recordings[1]
    assert rec.ended == "stale" and rec.stop == rec.start  # ended with its last event


def test_import_and_digest(lua, tmp_path):
    from wowgold import db
    from wowgold.play import recordings
    from wowgold.play.digest import digest

    g = lua.globals()
    g.slash("start hillsbrad")
    # two gryphon spawns, each killed three times ~5 min apart, plus bears in between
    for round_ in range(3):
        for uid, x in (("C1", 0.40), ("C2", 0.45)):
            g.px = x
            tick(lua, 60)
            g.kill(GRYPHON, f"{uid}{round_}", FEATHER, 1, "50 Copper")
        for b in range(3):
            tick(lua, 60)
            g.kill(BEAR, f"B{round_}{b}", None, 0)
    g.otherKill(GRYPHON, "F1")  # someone else got one
    tick(lua, 30)
    g.slash("stop")
    path = save(lua, tmp_path)

    conn = db.connect(tmp_path / "t.sqlite3")
    recs = recordings.read_file(path)
    assert [r.note for r in recs] == ["hillsbrad"]
    assert recordings.store(conn, recs).new == 1
    assert recordings.store(conn, recordings.read_file(path)).unchanged == 1
    (rec,) = recordings.load(conn)
    assert rec.cls == "hunter" and rec.level == 40 and rec.professions == {"skinning": 200}

    d = digest(rec)
    assert d.kills == 15 and d.duration_s == 15 * 60 + 30
    assert d.active_s == d.duration_s and d.paused_s == 0
    gry = next(s for s in d.mobs if s.id == GRYPHON)
    assert (gry.kills, gry.others, gry.loots) == (6, 1, 6)
    assert gry.items == {FEATHER: 6} and gry.copper == 300
    assert gry.respawn_pairs == 4 and gry.respawn_s == 300  # 5 min between re-kills
    assert d.copper_looted == 300 and d.items == {FEATHER: 6}
    assert len(d.kill_gaps) == 14


ITEMS = r"""
-- name, link, quality, ilvl, minlvl, type, subtype, stack, equip, texture, sellPrice, classID
local info = {
    [4589] = { "Long Elegant Feather", 1, 7, 50 },
    [4306] = { "Silk Cloth", 1, 7, 150 },
    [9999] = { "Green Sword", 2, 2, 2000 },
    [7090] = { "Pattern: Green Silk Armor", 2, 9, 300 },
}
function GetItemInfo(id)
    local i = info[id]
    if not i then return nil end
    return i[1], "[" .. i[1] .. "]", i[2], 1, 1, "", "", 20, "", 0, i[4], i[3]
end
Auctionator = { API = { v1 = { GetAuctionPriceByItemID = function(caller, id)
    if id == 4306 then return 1000 end
end } } }
"""


def test_running_summary_and_panel_digest(lua):
    g = lua.globals()
    lua.execute(ITEMS)
    g.slash("start")
    g.kill(GRYPHON, "A1", FEATHER, 3, "1 Silver")
    g.kill(GRYPHON, "A2", 4306, 4, "50 Copper")
    g.kill(GRYPHON, "A3", 9999, 1)
    g.kill(GRYPHON, "A4", 7090, 1)
    g.kill(GRYPHON, "A5", 1234, 1)  # not cached yet
    logger, ui = g.WowGoldLogger, g.WowGoldLogger.ui
    rec = logger.current()
    s = logger.summary(rec)
    assert (s.kills, s.copper, s["items"][FEATHER], s["items"][4306]) == (5, 150, 3, 4)
    # the same totals rebuilt from the event log (sessions recorded before the panel existed)
    rec.sum = None
    s2 = logger.summary(rec)
    assert (s2.kills, s2.copper, s2["items"][4306]) == (5, 150, 4)

    d = ui.digest(rec)
    assert d.count == 10 and d.pending
    assert d.vendor == 3 * 50 + 4 * 150 + 2000 + 300
    assert (d.ah, d.ahItems) == (4000, 1)  # only Silk Cloth has an Auctionator price
    mats = [d.mats[i].name for i in range(1, len(d.mats) + 1)]
    assert mats == ["Silk Cloth", "Long Elegant Feather"]  # by value: 4×1000 AH > 3×50 vendor
    notable = {d.notable[i].name for i in range(1, len(d.notable) + 1)}
    assert notable == {"Green Sword", "Pattern: Green Silk Armor"}

    g.slash("stop")
    assert logger.lastRecording().sum.kills == 5 and logger.current() is None


FAKE_WIDGETS = r"""
-- Any widget method works; text and visibility are remembered so the test can read them.
local Widget = {}
Widget.__index = function(self, key)
    if Widget[key] then return Widget[key] end
    return function() end
end
function Widget:SetText(t) rawset(self, "text", t) end
function Widget:GetText() return rawget(self, "text") end
function Widget:Show()
    rawset(self, "shown", true)
    local f = rawget(self, "OnShow")
    if f then f(self) end
end
function Widget:Hide() rawset(self, "shown", false) end
function Widget:IsShown() return rawget(self, "shown") or false end
function Widget:SetShown(v) rawset(self, "shown", v and true or false) end
function Widget:GetWidth() return 140 end
function Widget:SetScript(name, fn)
    rawset(self, name, fn)
    if name == "OnEvent" then self.onEvent = fn end
end
function Widget:RegisterEvent(e) self.events[e] = true end
function Widget:CreateFontString() return setmetatable({}, Widget) end
function Widget:CreateTexture() return setmetatable({}, Widget) end
function CreateFrame(kind, name)
    local f = setmetatable({ events = {}, kind = kind }, Widget)
    table.insert(frames, f)
    if name then _G[name] = f end
    return f
end
UIParent = setmetatable({}, Widget)
Minimap = setmetatable({}, Widget)
GameTooltip = setmetatable({}, Widget)
function GetCursorPosition() return 0, 0 end
"""


def test_panel_and_minimap_button_build(lua):
    g = lua.globals()
    lua.execute(ITEMS)
    lua.execute(FAKE_WIDGETS)
    g.fire("PLAYER_LOGIN")
    button = g.WowGoldLoggerMinimapButton
    assert button is not None and button.shown  # minimap button built and visible
    button.OnClick(button, "LeftButton")  # opens the panel
    panel = g.WowGoldLoggerPanel
    assert panel.shown and "Press Start" in panel.totals.text
    button.OnClick(button, "RightButton")  # starts recording
    assert g.WowGoldLogger.current() is not None and button.dot.shown
    g.kill(GRYPHON, "A1", 4306, 4, "1 Silver")
    tick(lua, 120)
    g.WowGoldLogger.ui.refresh()
    assert "Recording" in panel.status.text and "2:00" in panel.status.text
    assert "Raw gold" in panel.totals.text and "Kills: 1" in panel.totals.text
    assert "Auction value" in panel.totals.text and "[Silk Cloth]" in panel.mats.text
    button.OnEnter(button)  # tooltip
    panel.stop.OnClick(panel.stop)
    assert "Last session" in panel.status.text and not button.dot.shown
    g.slash("minimap")
    assert not button.shown
