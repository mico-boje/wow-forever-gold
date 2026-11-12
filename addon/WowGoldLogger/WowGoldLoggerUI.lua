-- WowGold Logger: minimap button and session panel.
--
-- The minimap button opens a small panel with the session timer, Start/Pause/Stop, and a
-- short summary: coins looted, kills, what the loot is worth (vendor, and the auction house if
-- Auctionator is installed), crafting materials and notable drops. When nothing is being
-- recorded it shows the last session.
--
-- Left-click the button: panel. Right-click: start / pause / resume. Drag: move it.

local Logger = WowGoldLogger
local UI = {}
Logger.ui = UI

local NOTABLE_QUALITY = 2 -- uncommon (green) and better
local CLASS_TRADEGOODS, CLASS_RECIPE = 7, 9
local LIST_LINES = 5
local WIDTH = 250

local panel, button

-- Helpers ---------------------------------------------------------------------------------

local function money(copper)
    copper = math.floor(copper or 0)
    if GetCoinTextureString then return GetCoinTextureString(copper) end
    return string.format("%dg %02ds %02dc", copper / 10000, (copper / 100) % 100, copper % 100)
end

local function clock(secs)
    secs = math.floor(secs or 0)
    if secs >= 3600 then
        return string.format("%d:%02d:%02d", secs / 3600, (secs / 60) % 60, secs % 60)
    end
    return string.format("%d:%02d", secs / 60, secs % 60)
end

local function perHour(n, secs)
    if not secs or secs < 60 then return "" end
    return n * 3600 / secs
end

-- name, link, quality, sellPrice, classID (nil until the client has the item cached)
local function itemInfo(id)
    local name, link, quality, _, _, _, _, _, _, _, sellPrice, classID = GetItemInfo(id)
    if not name and C_Item and C_Item.RequestLoadItemDataByID then
        C_Item.RequestLoadItemDataByID(id)
    end
    return name, link, quality, sellPrice, classID
end

-- Auction value per unit from Auctionator, if it's installed and has seen the item.
local function auctionPrice(id)
    local api = Auctionator and Auctionator.API and Auctionator.API.v1
    if not api or not api.GetAuctionPriceByItemID then return nil end
    local ok, price = pcall(api.GetAuctionPriceByItemID, "WowGoldLogger", id)
    return ok and price or nil
end

-- What a session's loot looks like: totals and the lists the panel shows.
function UI.digest(r)
    local sum = Logger.summary(r)
    local out = { vendor = 0, ah = 0, ahItems = 0, mats = {}, notable = {}, count = 0, pending = false }
    if not sum then return out end
    for id, n in pairs(sum.items) do
        out.count = out.count + n
        local name, link, quality, sellPrice, classID = itemInfo(id)
        if not name then
            out.pending = true
        else
            out.vendor = out.vendor + (sellPrice or 0) * n
            local ah = auctionPrice(id)
            if ah then
                out.ah = out.ah + ah * n
                out.ahItems = out.ahItems + 1
            end
            local entry = { id = id, n = n, name = name, link = link, value = (ah or sellPrice or 0) * n }
            if classID == CLASS_TRADEGOODS then
                table.insert(out.mats, entry)
            end
            if (quality or 0) >= NOTABLE_QUALITY or classID == CLASS_RECIPE then
                table.insert(out.notable, entry)
            end
        end
    end
    local byValue = function(a, b)
        if a.value ~= b.value then return a.value > b.value end
        return a.n > b.n
    end
    table.sort(out.mats, byValue)
    table.sort(out.notable, byValue)
    return out
end

local function listText(entries, empty)
    if #entries == 0 then return "|cff808080" .. empty .. "|r" end
    local lines = {}
    for i = 1, math.min(#entries, LIST_LINES) do
        local e = entries[i]
        lines[#lines + 1] = string.format("%d× %s", e.n, e.link or e.name)
    end
    if #entries > LIST_LINES then
        lines[#lines + 1] = string.format("|cff808080+%d more|r", #entries - LIST_LINES)
    end
    return table.concat(lines, "\n")
end

-- Panel ------------------------------------------------------------------------------------

local function fontString(parent, template, anchorTo, offsetY)
    local fs = parent:CreateFontString(nil, "OVERLAY", template or "GameFontHighlightSmall")
    fs:SetPoint("TOPLEFT", anchorTo, anchorTo == parent and "TOPLEFT" or "BOTTOMLEFT",
        anchorTo == parent and 12 or 0, offsetY or -6)
    fs:SetWidth(WIDTH - 24)
    fs:SetJustifyH("LEFT")
    if fs.SetWordWrap then fs:SetWordWrap(true) end
    return fs
end

local function makeButton(parent, text, width, onClick)
    local b = CreateFrame("Button", nil, parent, "UIPanelButtonTemplate")
    b:SetSize(width, 20)
    b:SetText(text)
    b:SetScript("OnClick", onClick)
    return b
end

local function buildPanel()
    local ok, f = pcall(CreateFrame, "Frame", "WowGoldLoggerPanel", UIParent, "BackdropTemplate")
    if not ok then f = CreateFrame("Frame", "WowGoldLoggerPanel", UIParent) end
    f:SetSize(WIDTH, 200)
    f:SetFrameStrata("MEDIUM")
    f:SetClampedToScreen(true)
    f:SetMovable(true)
    f:EnableMouse(true)
    f:RegisterForDrag("LeftButton")
    f:SetScript("OnDragStart", f.StartMoving)
    f:SetScript("OnDragStop", function(self)
        self:StopMovingOrSizing()
        local point, _, relPoint, x, y = self:GetPoint()
        Logger.db().panelPos = { point, relPoint, x, y }
    end)
    if f.SetBackdrop then
        f:SetBackdrop({
            bgFile = "Interface\\Tooltips\\UI-Tooltip-Background",
            edgeFile = "Interface\\Tooltips\\UI-Tooltip-Border",
            tile = true, tileSize = 16, edgeSize = 16,
            insets = { left = 4, right = 4, top = 4, bottom = 4 },
        })
        f:SetBackdropColor(0.05, 0.05, 0.07, 0.92)
        f:SetBackdropBorderColor(0.6, 0.5, 0.2, 1)
    end
    local pos = Logger.db().panelPos
    if pos then
        f:SetPoint(pos[1], UIParent, pos[2], pos[3], pos[4])
    else
        f:SetPoint("TOPRIGHT", UIParent, "TOPRIGHT", -200, -220)
    end

    f.title = fontString(f, "GameFontNormal", f, -10)
    f.title:SetText("WowGold session")
    local close = CreateFrame("Button", nil, f, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", f, "TOPRIGHT", 0, 0)
    close:SetScript("OnClick", function() f:Hide() end)

    f.status = fontString(f, "GameFontHighlight", f.title, -6)

    f.start = makeButton(f, "Start", 70, function()
        local r = Logger.current()
        if not r then Logger.start("")
        elseif r.paused then Logger.resume()
        else Logger.pause() end
    end)
    f.start:SetPoint("TOPLEFT", f.status, "BOTTOMLEFT", 0, -6)
    f.stop = makeButton(f, "Stop", 70, function() Logger.stop() end)
    f.stop:SetPoint("LEFT", f.start, "RIGHT", 6, 0)

    f.totals = fontString(f, "GameFontHighlightSmall", f.start, -8)
    f.matsLabel = fontString(f, "GameFontNormalSmall", f.totals, -8)
    f.matsLabel:SetText("Crafting materials")
    f.mats = fontString(f, "GameFontHighlightSmall", f.matsLabel, -2)
    f.notableLabel = fontString(f, "GameFontNormalSmall", f.mats, -8)
    f.notableLabel:SetText("Notable loot")
    f.notable = fontString(f, "GameFontHighlightSmall", f.notableLabel, -2)
    f.hint = fontString(f, "GameFontDisableSmall", f.notable, -8)

    -- the timer ticks every second while the panel is open
    local since = 0
    f:SetScript("OnUpdate", function(_, e)
        since = since + e
        if since >= 1 then
            since = 0
            UI.refresh()
        end
    end)
    f:SetScript("OnShow", function() UI.refresh() end)
    f:Hide()
    return f
end

function UI.refresh()
    UI.refreshButton()
    if not panel or not panel:IsShown() then return end
    local r = Logger.current()
    local shown = r or Logger.lastRecording()
    if r then
        panel.status:SetText(string.format("%s  %s%s",
            r.paused and "|cffaaaaaaPaused|r" or "|cffff4040Recording|r", clock(Logger.elapsed(r)),
            r.note and ("  |cff808080" .. r.note .. "|r") or ""))
        panel.start:SetText(r.paused and "Resume" or "Pause")
        panel.stop:Enable()
    else
        panel.status:SetText(shown and ("|cff808080Last session|r  " .. clock(Logger.elapsed(shown))
            .. (shown.note and ("  |cff808080" .. shown.note .. "|r") or ""))
            or "|cff808080Not recording|r")
        panel.start:SetText("Start")
        panel.stop:Disable()
    end
    if not shown then
        panel.totals:SetText("Press Start (or /wgl start <note>) before you farm.")
        panel.mats:SetText("")
        panel.notable:SetText("")
        panel.hint:SetText("")
    else
        local sum = Logger.summary(shown)
        local d = UI.digest(shown)
        local secs = Logger.elapsed(shown)
        local lines = {
            string.format("Raw gold: %s", money(sum.copper)),
            string.format("Kills: %d%s", sum.kills,
                secs >= 60 and string.format("  (%.0f/h)", perHour(sum.kills, secs)) or ""),
            string.format("Items: %d  ·  vendor %s", d.count, money(d.vendor)),
        }
        if d.ahItems > 0 then
            lines[#lines + 1] = string.format("Auction value: %s |cff808080(Auctionator)|r", money(d.ah))
        end
        local total = sum.copper + (d.ahItems > 0 and d.ah or d.vendor)
        if secs >= 60 then
            lines[#lines + 1] = string.format("|cffffd100~%s per hour|r", money(total * 3600 / secs))
        end
        panel.totals:SetText(table.concat(lines, "\n"))
        panel.mats:SetText(listText(d.mats, "none yet"))
        panel.notable:SetText(listText(d.notable, "nothing green or better yet"))
        panel.hint:SetText(r and "Stop, then /reload to send it to wowgold."
            or (d.pending and "Loading item info..." or "Full numbers: Sessions tab in wowgold."))
    end
    -- grow the panel to fit its text
    local top = panel:GetTop()
    local bottom = panel.hint:GetBottom()
    if top and bottom then panel:SetHeight(math.max(120, top - bottom + 12)) end
end

function UI.panel(what)
    panel = panel or buildPanel()
    if what == "hide" or (what == "toggle" and panel:IsShown()) then
        panel:Hide()
    else
        panel:Show()
    end
end

-- Minimap button ----------------------------------------------------------------------------

local function placeButton()
    local angle = math.rad(Logger.db().minimapAngle or 210)
    local radius = (Minimap:GetWidth() / 2) + 10
    button:ClearAllPoints()
    button:SetPoint("CENTER", Minimap, "CENTER", math.cos(angle) * radius, math.sin(angle) * radius)
end

local function buildButton()
    local b = CreateFrame("Button", "WowGoldLoggerMinimapButton", Minimap)
    b:SetSize(31, 31)
    b:SetFrameStrata("MEDIUM")
    b:SetFrameLevel(8)
    b:SetHighlightTexture("Interface\\Minimap\\UI-Minimap-ZoomButton-Highlight")
    local overlay = b:CreateTexture(nil, "OVERLAY")
    overlay:SetSize(53, 53)
    overlay:SetTexture("Interface\\Minimap\\MiniMap-TrackingBorder")
    overlay:SetPoint("TOPLEFT")
    local bg = b:CreateTexture(nil, "BACKGROUND")
    bg:SetSize(20, 20)
    bg:SetTexture("Interface\\Minimap\\UI-Minimap-Background")
    bg:SetPoint("TOPLEFT", 7, -5)
    b.icon = b:CreateTexture(nil, "ARTWORK")
    b.icon:SetSize(17, 17)
    b.icon:SetTexture("Interface\\Icons\\INV_Misc_Coin_01")
    b.icon:SetPoint("TOPLEFT", 7, -6)
    b.dot = b:CreateTexture(nil, "OVERLAY", nil, 7)
    b.dot:SetSize(10, 10)
    b.dot:SetTexture("Interface\\COMMON\\Indicator-Red")
    b.dot:SetPoint("BOTTOMRIGHT", -4, 5)
    b.dot:Hide()

    b:RegisterForClicks("LeftButtonUp", "RightButtonUp")
    b:SetScript("OnClick", function(_, mouse)
        if mouse == "RightButton" then
            local r = Logger.current()
            if not r then Logger.start("")
            elseif r.paused then Logger.resume()
            else Logger.pause() end
        else
            UI.panel("toggle")
        end
    end)
    b:RegisterForDrag("LeftButton")
    b:SetScript("OnDragStart", function(self)
        self:SetScript("OnUpdate", function()
            local mx, my = Minimap:GetCenter()
            local cx, cy = GetCursorPosition()
            local scale = Minimap:GetEffectiveScale()
            Logger.db().minimapAngle = math.deg(math.atan2(cy / scale - my, cx / scale - mx))
            placeButton()
        end)
    end)
    b:SetScript("OnDragStop", function(self) self:SetScript("OnUpdate", nil) end)
    b:SetScript("OnEnter", function(self)
        GameTooltip:SetOwner(self, "ANCHOR_LEFT")
        GameTooltip:AddLine("WowGold Logger")
        local r = Logger.current()
        if r then
            local sum = Logger.summary(r)
            GameTooltip:AddLine(string.format("%s %s · %d kills · %s",
                r.paused and "Paused" or "Recording", clock(Logger.elapsed(r)), sum.kills,
                money(sum.copper)), 1, 1, 1)
        else
            GameTooltip:AddLine("Not recording", 0.6, 0.6, 0.6)
        end
        GameTooltip:AddLine("Left-click: session panel", 0.6, 0.6, 0.6)
        GameTooltip:AddLine(r and (r.paused and "Right-click: resume" or "Right-click: pause")
            or "Right-click: start recording", 0.6, 0.6, 0.6)
        GameTooltip:Show()
    end)
    b:SetScript("OnLeave", function() GameTooltip:Hide() end)
    return b
end

function UI.refreshButton()
    if not button then return end
    local r = Logger.current()
    button.dot:SetShown(r ~= nil and not r.paused)
    if button.icon.SetDesaturated then button.icon:SetDesaturated(r ~= nil and r.paused) end
end

function UI.minimap()
    local db = Logger.db()
    db.hideMinimap = not db.hideMinimap
    if button then button:SetShown(not db.hideMinimap) end
    DEFAULT_CHAT_FRAME:AddMessage("|cffe6c200WowGold|r minimap button "
        .. (db.hideMinimap and "hidden (/wgl minimap to bring it back)" or "shown"))
end

-- Setup --------------------------------------------------------------------------------------

Logger.onChange = function() UI.refresh() end

local setup = CreateFrame("Frame")
setup:RegisterEvent("PLAYER_LOGIN")
setup:SetScript("OnEvent", function()
    button = buildButton()
    placeButton()
    button:SetShown(not Logger.db().hideMinimap)
    UI.refreshButton()
    -- reopen the panel after a /reload mid-session
    if Logger.current() then UI.panel("show") end
end)
