"""Run the WowGoldLogger addon in an embedded Lua 5.1 with a stubbed WoW API, then feed
its SavedVariables through the Python importer."""

from __future__ import annotations

from pathlib import Path

import pytest

lua51 = pytest.importorskip("lupa.lua51")

ADDON = Path(__file__).parent.parent / "addon" / "WowGoldLogger" / "WowGoldLogger.lua"

WOW_STUBS = r"""
LOOT_ITEM_SELF = "You receive loot: %s."
LOOT_ITEM_SELF_MULTIPLE = "You receive loot: %sx%d."
GOLD_AMOUNT = "%d Gold"
SILVER_AMOUNT = "%d Silver"
COPPER_AMOUNT = "%d Copper"
messages = {}
DEFAULT_CHAT_FRAME = { AddMessage = function(self, m) table.insert(messages, m) end }
clock = 1762850000
zone = "Felwood"
lootGuid = nil
cleu = {}
function time() return clock end
function UnitClass() return "Druid", "DRUID" end
function UnitLevel() return 60 end
function UnitGUID(unit) if unit == "player" then return "Player-1-00000001" end return nil end
function GetZoneText() return zone end
C_Map = { GetBestMapForUnit = function() return 1448 end }
function GetNumLootItems() return lootGuid and 1 or 0 end
function GetLootSourceInfo(slot) return lootGuid, 1 end
function CombatLogGetCurrentEventInfo() return unpack(cleu) end
SlashCmdList = {}
handler = nil
function CreateFrame()
    return {
        RegisterEvent = function() end,
        SetScript = function(self, name, fn) handler = fn end,
    }
end
function fire(event, ...) handler(nil, event, ...) end
function slash(text) SlashCmdList["WOWGOLDLOGGER"](text) end

-- Helpers that play out common in-game sequences.
function link(id, name)
    return "|cffffffff|Hitem:" .. id .. "::::::::60:::::|h[" .. name .. "]|h|r"
end
function gather(spell, objectId, itemId, count)
    fire("UNIT_SPELLCAST_SUCCEEDED", "player", "Cast-1", spell)
    lootGuid = "GameObject-0-4372-0-17-" .. objectId .. "-0000ABCDEF"
    fire("LOOT_OPENED")
    fire("CHAT_MSG_LOOT", "You receive loot: " .. link(itemId, "x") .. "x" .. count .. ".")
    fire("LOOT_CLOSED")
end
function killAndLoot(npcId, itemId, count, money)
    local guid = "Creature-0-4372-0-17-" .. npcId .. "-0000ABCDEF"
    cleu = { 0, "PARTY_KILL", false, "Player-1-00000001", "Me", 0, 0, guid }
    fire("COMBAT_LOG_EVENT_UNFILTERED")
    lootGuid = guid
    fire("LOOT_OPENED")
    if itemId then
        fire("CHAT_MSG_LOOT", "You receive loot: " .. link(itemId, "x") .. "x" .. count .. ".")
    end
    if money then fire("CHAT_MSG_MONEY", "You loot " .. money) end
    fire("LOOT_CLOSED")
end
"""

HERB_GATHERING = 11993
DREAMFOIL_NODE = 176584


@pytest.fixture
def lua():
    rt = lua51.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(WOW_STUBS)
    rt.execute(ADDON.read_text())
    rt.globals().fire("ADDON_LOADED", "WowGoldLogger")
    return rt


def tick(lua, seconds):
    lua.globals().clock += seconds


def test_patterns(lua):
    logger = lua.globals().WowGoldLogger
    dreamfoil = lua.globals().link(13463, "Dreamfoil")
    assert logger.parseLoot(f"You receive loot: {dreamfoil}x3.") == (13463, 3)
    assert logger.parseLoot(f"You receive loot: {dreamfoil}.") == (13463, 1)
    assert logger.parseLoot("Bob receives loot: [Dreamfoil].") is None
    assert logger.parseMoney("You loot 1 Gold, 20 Silver, 5 Copper") == 12005
    assert logger.toPattern("Beute: %1$sx%2$d.") == "^Beute: (.+)x(%d+)%.$"
    assert logger.parseGuid("Creature-0-4372-0-17-7105-0000ABCDEF") == ("Creature", 7105)
    assert logger.parseGuid("GameObject-0-4372-0-17-181270-0000ABCDEF") == ("GameObject", 181270)
    assert logger.parseGuid("Player-4372-0A1B2C3D") is None


def test_records_sources_spells_kills_and_active_time(lua):
    g = lua.globals()
    for _ in range(3):
        g.gather(HERB_GATHERING, DREAMFOIL_NODE, 13463, 2)
        tick(lua, 60)
    g.killAndLoot(7105, 14047, 2, "15 Silver")
    tick(lua, 2)
    # Loot shortly after the window closed still belongs to the satyr...
    g.fire("CHAT_MSG_LOOT", "You receive loot: " + g.link(14256, "Felcloth") + ".")
    tick(lua, 30)
    # ...but not half a minute later.
    g.fire("CHAT_MSG_LOOT", "You receive loot: " + g.link(9999, "Mystery") + ".")
    # Changing zone closes the segment on the next activity.
    g.zone = "Winterspring"
    tick(lua, 60)
    g.gather(HERB_GATHERING, 1, 13467, 1)

    db = g.WowGoldLoggerDB
    seg = db.segments[1]
    assert seg.zone == "Felwood" and seg.closed == "zone" and seg.map == 1448
    node = seg.sources[f"GameObject:{DREAMFOIL_NODE}"]
    assert (node.loots, node["items"][13463], node.via) == (3, 6, HERB_GATHERING)
    satyr = seg.sources["Creature:7105"]
    assert satyr["items"][14047] == 2 and satyr["items"][14256] == 1 and satyr.copper == 1500
    assert seg.sources["unknown"]["items"][9999] == 1
    assert seg.kills[7105] == 1
    assert seg.copper == 1500
    assert seg.active == 60 * 3 + 2 + 30  # time between events, each gap capped at 5 min
    assert db.open.zone == "Winterspring"


def test_idle_gap_pause_and_labels(lua):
    g = lua.globals()
    g.gather(HERB_GATHERING, DREAMFOIL_NODE, 13463, 1)
    tick(lua, 61)
    g.gather(HERB_GATHERING, DREAMFOIL_NODE, 13463, 1)
    tick(lua, g.WowGoldLogger.IDLE_GAP + 1)
    g.gather(HERB_GATHERING, DREAMFOIL_NODE, 13463, 1)  # after a long break: new segment
    db = g.WowGoldLoggerDB
    assert len(db.segments) == 1 and db.segments[1].closed == "idle"

    g.slash("pause")
    g.gather(HERB_GATHERING, DREAMFOIL_NODE, 13463, 5)
    assert db.open is None  # nothing recorded while paused
    g.slash("resume")

    g.slash("start herb_felwood")
    g.gather(HERB_GATHERING, DREAMFOIL_NODE, 13463, 1)
    assert db.open.label == "herb_felwood"
    tick(lua, 120)
    g.gather(HERB_GATHERING, DREAMFOIL_NODE, 13463, 1)
    g.slash("stop")
    assert db.open is None and db.segments[len(db.segments)].label == "herb_felwood"


def test_reload_keeps_recent_segment_open(lua):
    g = lua.globals()
    g.gather(HERB_GATHERING, DREAMFOIL_NODE, 13463, 1)
    tick(lua, 120)
    g.fire("ADDON_LOADED", "WowGoldLogger")  # /reload shortly after: same segment
    assert g.WowGoldLoggerDB.open is not None
    tick(lua, g.WowGoldLogger.IDLE_GAP + 1)
    g.fire("ADDON_LOADED", "WowGoldLogger")  # logged back in much later: closed
    assert g.WowGoldLoggerDB.open is None and len(g.WowGoldLoggerDB.segments) == 1


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


def test_recorded_herbing_is_matched_to_the_felwood_route(lua, tmp_path):
    from wowgold import db
    from wowgold.methods.models import load_methods
    from wowgold.planner.profile import CharacterProfile
    from wowgold.sessions import import_addon_sessions

    g = lua.globals()
    herbs = [(13463, 2), (8846, 2), (13466, 1), (13465, 1)]  # Felwood route herbs
    for n in range(20):
        item_id, count = herbs[n % len(herbs)]
        g.gather(HERB_GATHERING, 1000 + item_id, item_id, count)
        tick(lua, 60)
    g.slash("pause")  # closes the segment
    saved = tmp_path / "WowGoldLogger.lua"
    saved.write_text("WowGoldLoggerDB = " + _serialize(g.WowGoldLoggerDB) + "\n")

    methods = {m.id: m for m in load_methods(Path(__file__).parent.parent / "data" / "methods")}
    profile = CharacterProfile(**{"class": "mage"}, level=50, faction="horde")
    conn = db.connect(tmp_path / "t.sqlite3")
    summary = import_addon_sessions(conn, saved, methods, profile)

    [result] = summary.segments
    assert result.proposal.status == "auto"
    assert [a.method_id for a in result.proposal.assignments] == ["herb_felwood"]
    row = conn.execute("SELECT * FROM sessions").fetchone()
    assert (row["method_id"], row["cls"], row["level"]) == ("herb_felwood", "druid", 60)
    assert row["duration_s"] == 19 * 60
    assert row["multiplier"] == pytest.approx(1.15)  # druid modifier for herb routes
    status = conn.execute("SELECT status FROM addon_segments").fetchone()[0]
    assert status == "applied"
    # Re-importing the same file changes nothing.
    again = import_addon_sessions(conn, saved, methods, profile)
    assert again.segment_duplicates == 1 and not again.segments
