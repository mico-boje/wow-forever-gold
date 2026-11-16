-- WowGold Logger: farming routes sent from wowgold, shown with RXPGuides.
--
-- Routes.lua (written by the wowgold app: Farms page > Send to game) lists the routes. Each
-- becomes an RXPGuides guide in the "WowGold farms" group: one `.loop` step that draws the
-- route on the map and points RXP's arrow along it. Routes from vanilla spawn data come in
-- world coordinates and are converted to map coordinates with the game's own map API.
--
--   /wgl routes    list the routes and whether RXPGuides picked them up

local Logger = WowGoldLogger
local R = {}
Logger.routes = R

R.GROUP = "WowGold farms"

local function round1(v) return math.floor(v * 10 + 0.5) / 10 end

local function xy(pos)
    if not pos then return nil end
    if pos.GetXY then return pos:GetXY() end
    return pos.x, pos.y
end

local function project(continent, wx, wy, uiMap)
    local ok, mapId, pos = pcall(C_Map.GetMapPosFromWorldPos, continent, CreateVector2D(wx, wy), uiMap)
    if not ok or not mapId then return nil end
    local x, y = xy(pos)
    if not x or not y then return nil end
    return mapId, x, y
end

local ZONE = Enum and Enum.UIMapType and Enum.UIMapType.Zone or 3

-- The zone map a world route belongs on. Without a target map the game places world positions
-- on the continent map (Kalimdor), where RXP only draws the loop while the whole continent is
-- shown. Find the zone: by the route's zone name among the continent's zones, else whatever
-- zone lies under the route's centre.
function R.zoneMap(route, continentMap, cx, cy)
    if C_Map.GetMapChildrenInfo then
        local ok, kids = pcall(C_Map.GetMapChildrenInfo, continentMap, ZONE, true)
        for _, info in ipairs(ok and kids or {}) do
            if info.name == route.zone then return info.mapID end
        end
    end
    if C_Map.GetMapInfoAtPosition then
        local ok, info = pcall(C_Map.GetMapInfoAtPosition, continentMap, cx, cy)
        if ok and info and info.mapID ~= continentMap then return info.mapID end
    end
    return continentMap
end

-- (uiMapID, {x1, y1, x2, y2, ...} in percent) for a route, or nil if it can't be placed.
function R.mapCoords(route)
    local pts = route.points or {}
    if route.kind == "map" then
        if #pts < 2 then return nil end
        return route.map, pts
    end
    if not (C_Map and C_Map.GetMapPosFromWorldPos and CreateVector2D) then return nil end
    -- where the route sits on the continent map, to find its zone
    local base, sx, sy, n = nil, 0, 0, 0
    for i = 1, #pts - 1, 2 do
        local mapId, x, y = project(route.map, pts[i], pts[i + 1])
        if mapId then
            base = base or mapId
            sx, sy, n = sx + x, sy + y, n + 1
        end
    end
    if not base then return nil end
    local zone = R.zoneMap(route, base, sx / n, sy / n)
    local out = {}
    for i = 1, #pts - 1, 2 do
        local x, y = R.toZone(route.map, zone, pts[i], pts[i + 1])
        if x and x >= 0 and x <= 1 and y >= 0 and y <= 1 then
            out[#out + 1] = round1(x * 100)
            out[#out + 1] = round1(y * 100)
        end
    end
    if #out < 2 then return nil end
    return zone, out
end

-- A database world position (x north, y west, in yards) on a zone map (0-1).
-- HereBeDragons (bundled with RXPGuides) does this the way RXP draws its own routes; Forever's
-- GetMapPosFromWorldPos with a target map gave positions far off. HereBeDragons names the axes
-- the other way round: its world x is the database y.
function R.toZone(continent, zone, wx, wy)
    local HBD = LibStub and LibStub("HereBeDragons-2.0", true)
    if HBD and HBD.GetZoneCoordinatesFromWorld then
        local ok, x, y = pcall(HBD.GetZoneCoordinatesFromWorld, HBD, wy, wx, zone, true)
        if ok and x and y then return x, y end
    end
    local mapId, x, y = project(continent, wx, wy, zone)
    if mapId then return x, y end
    return nil
end

local function clean(text)
    return (tostring(text or ""):gsub("[\r\n]", " "):gsub("%]%]", "] ]"))
end

function R.guideName(route) return "WowGold: " .. clean(route.name) end

-- RXPGuides guide text: one step that opens the zone's map and loops the route. RXP draws the
-- loop on the world map while that zone's map is shown (and on the minimap once you're in the
-- zone), but only while the step is open: a step with nothing to complete counts as done, so
-- it "collects" 9999 of the farm's main item (RXP shows the count as you loot).
R.COLLECT_GOAL = 9999

function R.guideText(route, mapId, coords)
    local note = clean(route.note ~= "" and route.note or route.name)
    local lines = {
        "#group " .. R.GROUP,
        "#name " .. R.guideName(route) .. " (" .. clean(route.id) .. ")",
        "#displayname " .. R.guideName(route),
        "step",
        "    .openmap " .. mapId .. " >>The route is drawn on this map",
    }
    -- the mobs to kill: RXP lists them as targets (and in its targeting macro)
    local mobs = {}
    for _, name in ipairs(route.mobs or {}) do
        name = clean(name):gsub(",", "")
        if name ~= "" then mobs[#mobs + 1] = name end
    end
    for i, name in ipairs(mobs) do
        lines[#lines + 1] = "    .mob " .. name
            .. (i == 1 and (" >>Kill " .. table.concat(mobs, ", ")) or "")
    end
    if route.item then
        lines[#lines + 1] = "    .collect " .. route.item .. "," .. R.COLLECT_GOAL
    else
        lines[#lines + 1] = "    .xp 61 >>Farm the route" -- can't be reached: keeps the step open
    end
    lines[#lines + 1] = "    .loop " .. (route.range or 25) .. "," .. mapId .. ","
        .. table.concat(coords, ",") .. " >>" .. note
    return table.concat(lines, "\n")
end

local function mapName(mapId)
    local info = C_Map and C_Map.GetMapInfo and C_Map.GetMapInfo(mapId)
    return info and info.name or "?"
end

-- Register every route with RXPGuides. Returns (added, problems) or nil without RXPGuides.
function R.register()
    local routes = WowGoldLoggerRoutes or {}
    R.status = {}
    if not (RXPGuides and RXPGuides.RegisterGuide) then return nil end
    local added, problems = 0, 0
    for _, route in ipairs(routes) do
        local mapId, coords = R.mapCoords(route)
        local ok, err = false, "couldn't place it on a map"
        if mapId then ok, err = pcall(RXPGuides.RegisterGuide, R.guideText(route, mapId, coords)) end
        R.status[route.id] = ok
            and string.format("on the %s map (%d), %d stops, first at %.1f, %.1f", mapName(mapId),
                mapId, #coords / 2, coords[1], coords[2])
            or tostring(err)
        if ok then added = added + 1 else problems = problems + 1 end
    end
    return added, problems
end

function R.list()
    local say = function(m) DEFAULT_CHAT_FRAME:AddMessage("|cffe6c200WowGold|r " .. m) end
    local routes = WowGoldLoggerRoutes or {}
    if #routes == 0 then
        return say("no routes yet: Farms page in wowgold > Send to game, then /reload")
    end
    if not (RXPGuides and RXPGuides.RegisterGuide) then
        say("RXPGuides isn't loaded, so routes can't be shown")
    end
    for i, route in ipairs(routes) do
        say(string.format("%d. %s (%s): %s", i, route.name, route.zone or "?",
            (R.status and R.status[route.id]) or "not registered"))
    end
    say("In RXPGuides: open the guide list > " .. R.GROUP .. ". The route is drawn on the world "
        .. "map when it shows that zone, and on the minimap once you're there.")
end

-- RXPGuides loads first (OptionalDeps), so its guides and ours register at the same time.
local added, problems = R.register()
local login = CreateFrame("Frame")
login:RegisterEvent("PLAYER_LOGIN")
login:SetScript("OnEvent", function()
    local n = #(WowGoldLoggerRoutes or {})
    if n == 0 then return end
    local say = function(m) DEFAULT_CHAT_FRAME:AddMessage("|cffe6c200WowGold|r " .. m) end
    if added == nil then
        say(n .. " farm route(s) waiting, but RXPGuides isn't loaded")
    else
        say(string.format("%d farm route(s) in RXPGuides > %s%s", added, R.GROUP,
            problems > 0 and string.format(" (%d couldn't be placed: /wgl routes)", problems) or ""))
    end
end)
