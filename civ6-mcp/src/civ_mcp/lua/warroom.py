"""War-room resident framework (injected into the game's InGame Lua context).

The bridge cannot install a mod, but it CAN inject persistent globals into a
Lua context over FireTuner — the same trick the World Congress handler uses.
The framework:

  * hooks a curated list of gameplay events into a ring buffer
  * discovers the REAL event-name list of this game build once (pairs(Events))
  * self-heals: the drain script re-injects the whole framework when the
    context reloaded (load / new game) and __wr vanished

Protocol (printed lines, pipe format like every other builder):
  EVT|<turn>|<event name>|<args flattened>   — buffered game events
  EVNAME|<event name>                        — discovered names (first drain)
  WR_OK|<version>|<hooked count>             — drain footer
"""

from civ_mcp.lua._helpers import SENTINEL


def build_wr_preflight() -> str:
    """ONE InGame roundtrip covering every cheap end_turn entry check:
    alive / open diplomacy sessions / incoming deals / World Congress
    proximity+handler / dedication selections allowed.

    Output: ``PF|<alive 0/1>|<diplo N>|<deals N>|<wcTurns N>|<handler 0/1>|<dedication N>``
    Callers run the full (expensive) specific check only for a tripped flag.
    """
    return f"""
local me = Game.GetLocalPlayer()
local alive = (Players[me] ~= nil and Players[me]:IsAlive()) and 1 or 0
local diploOpen = 0
local dealCount = 0
for i = 0, 62 do
  if i ~= me and Players[i] and Players[i]:IsAlive() then
    local sid = DiplomacyManager.FindOpenSessionID(me, i)
    if sid and sid >= 0 then
      diploOpen = diploOpen + 1
      local ok, deal = pcall(function()
        return DealManager.GetWorkingDeal(DealDirection.INCOMING, me, i) end)
      if ok and deal then
        local c = deal:GetItemCount()
        if c and c > 0 then dealCount = dealCount + 1 end
      end
    end
  end
end
local wcTurns = 99
pcall(function()
  local wc = Game.GetWorldCongress()
  if wc then
    local mtg = wc:GetMeetingStatus()
    if mtg and mtg.TurnsLeft and mtg.TurnsLeft <= 0 then wcTurns = 0 end
  end
end)
local wcHandler = (__civmcp_wc_handler ~= nil) and 1 or 0
local dedAllowed = 0
pcall(function()
  dedAllowed = Game.GetEras():GetPlayerNumAllowedCommemorations(me) or 0
end)
local gpClaim = 0
pcall(function()
  local gp = Game.GetGreatPeople()
  if gp then
    local timeline = gp:GetTimeline()
    if timeline then
      for _, entry in ipairs(timeline) do
        if gp:CanRecruitPerson(me, entry.Individual) then
          gpClaim = gpClaim + 1
        end
      end
    end
  end
end)
local pfEmptySlots = 0
pcall(function()
  local pCulture = Players[me]:GetCulture()
  for s = 0, pCulture:GetNumPolicySlots() - 1 do
    if pCulture:GetSlotPolicy(s) < 0 then pfEmptySlots = pfEmptySlots + 1 end
  end
end)
local govTitles = 0
pcall(function()
  local pGovs = Players[me]:GetGovernors()
  -- GetGovernorPoints() = lifetime earned; spendable = earned - spent
  govTitles = (pGovs:GetGovernorPoints() - pGovs:GetGovernorPointsSpent()) or 0
  if govTitles < 0 then govTitles = 0 end
end)
print("PF|" .. alive .. "|" .. diploOpen .. "|" .. dealCount .. "|"
  .. wcTurns .. "|" .. wcHandler .. "|" .. dedAllowed .. "|" .. gpClaim
  .. "|" .. pfEmptySlots .. "|" .. govTitles)
-- popup sweep (same checks the standalone dismiss used; DISMISSED| lines
-- are ignored by the PF parser)
do
  local names = {{"InGamePopup","GenericPopup","PopupDialog",
    "BoostUnlockedPopup","GreatWorkShowcase","WorldCongressPopup",
    "WorldCongressIntro"}}
  for _, nm in ipairs(names) do
    local c = ContextPtr:LookUpControl("/InGame/" .. nm)
    if c and not c:IsHidden() then
      pcall(function() UIManager:DequeuePopup(c) end)
      pcall(function() Input.PopContext() end)
      c:SetHide(true)
      print("DISMISSED|" .. nm)
    end
  end
end
print("{SENTINEL}")
"""


def parse_wr_preflight(lines: list[str]) -> dict:
    """Parse the PF| line; tolerant defaults when absent."""
    out = {"alive": True, "diplo": 0, "deals": 0,
           "wc_imminent": False, "wc_handler": False, "dedication": 0,
           "gp": 0, "empty_slots": 0, "gov_titles": 0}
    for line in lines:
        if line.startswith("PF|"):
            parts = line.split("|")
            if len(parts) >= 7:
                out["alive"] = parts[1] == "1"
                out["diplo"] = int(parts[2])
                out["deals"] = int(parts[3])
                out["wc_imminent"] = int(parts[4]) <= 0
                out["wc_handler"] = parts[5] == "1"
                out["dedication"] = int(parts[6])
            if len(parts) >= 8:
                out["gp"] = int(parts[7])
            if len(parts) >= 9:
                out["empty_slots"] = int(parts[8])
            if len(parts) >= 10:
                out["gov_titles"] = int(parts[9])
    return out

# Gameplay-relevant events. Names that don't exist in this build are skipped
# silently (ev == nil) — the discovery dump tells us what actually exists,
# and the curated list is extended from what it reports.
_WR_CURATED_EVENTS = [
    # turn lifecycle
    "LocalPlayerTurnBegin", "LocalPlayerTurnEnd",
    "TurnBegin", "TurnEnd",
    "PlayerTurnActivated",
    # combat & units
    "UnitKilledInCombat", "UnitCaptured",
    "UnitAddedToMap", "UnitRemovedFromMap",
    "UnitOperationCompleted", "UnitOperationStarted",
    "UnitGreatPersonActivated", "UnitPromoted",
    "UnitUpgraded",
    "GoodyHutReceived",
    # cities & builds
    "CityAddedToMap", "CityRemovedFromMap", "CityInitialized",
    "CityOccupied", "CityLiberated", "CityDestroyed",
    "WonderCompleted", "ProjectCompleted",
    "DistrictProgressChanged",
    # science / culture / era
    "ResearchCompleted", "CivicCompleted",
    "TechBoostTriggered", "CivicBoostTriggered",
    "EraReached", "EraProgressChanged",
    "GreatPersonCreated", "GreatWorkCreated",
    "PolicyBlockChanged",
    # diplomacy & world congress
    "DiplomacySessionOpened", "DiplomacySessionClosed",
    "DiplomacyDeclareWarMade", "DiplomacyPeaceMade",
    "DiplomacyDealAccepted",
    "WorldCongressEnter", "WorldCongressConclude",
    "QuestChanged", "QuestComplete",
    "InfluenceChanged",
    # endgame
    "PlayerDefeat", "PlayerVictory", "PlayerEliminated",
]

_WR_EVENTS_LUA_LIST = ",\n    ".join(f'"{n}"' for n in _WR_CURATED_EVENTS)

_WR_FRAMEWORK_LUA = f"""
local __wrEvents = {{
    {_WR_EVENTS_LUA_LIST}
}}
if __wr ~= nil and __wr.hooked then
    print("WR_OK|" .. __wr.ver .. "|already")
else
    __wr = {{ events = {{}}, evcap = 400, hooked = 0, ver = 1, names = nil }}

    local function wrPush(name, detail)
        local turn = Game.GetCurrentGameTurn and Game.GetCurrentGameTurn() or -1
        if #__wr.events >= __wr.evcap then table.remove(__wr.events, 1) end
        table.insert(__wr.events, turn .. "|" .. name .. "|" .. tostring(detail and detail or ""))
    end

    local function wrArgs(...)
        local parts = {{}}
        for i = 1, select("#", ...) do parts[#parts + 1] = tostring(select(i, ...)) end
        return table.concat(parts, ",")
    end

    -- hook the curated names from BOTH event tables (Events = UI side,
    -- GameEvents = gameplay side; availability varies by name)
    local function wrHook(tbl, label)
        if tbl == nil then return end
        for _, name in ipairs(__wrEvents) do
            local ev = tbl[name]
            if ev ~= nil then
                local ok = pcall(function()
                    ev.Add(function(...)
                        wrPush(label .. name, wrArgs(...))
                    end)
                end)
                if ok then __wr.hooked = __wr.hooked + 1 end
            end
        end
    end
    wrHook(Events, "")
    wrHook(GameEvents, "G:")

    -- one-time discovery of every event name this build actually exposes
    pcall(function()
        local names = {{}}
        for name, _ in pairs(Events) do names[#names + 1] = name end
        if GameEvents then
            for name, _ in pairs(GameEvents) do names[#names + 1] = "G:" .. name end
        end
        table.sort(names)
        __wr.names = names
    end)

    print("WR_OK|" .. __wr.ver .. "|injected:" .. __wr.hooked)
end
"""


def build_wr_drain(max_events: int = 200) -> str:
    """InGame script: self-heal the resident framework, then drain the event
    buffer. Prints EVT|/EVNAME|/WR_OK| lines, one roundtrip total."""
    return (
        _WR_FRAMEWORK_LUA
        + f"""
local n = 0
for _, e in ipairs(__wr.events) do
    if n < {max_events} then print("EVT|" .. e); n = n + 1 end
end
if __wr.names ~= nil then
    for _, name in ipairs(__wr.names) do print("EVNAME|" .. name) end
    __wr.names = nil
end
__wr.events = {{}}
print("{SENTINEL}")
"""
    )


def parse_wr_lines(lines: list[str]) -> dict:
    """Split a drain response into events / discovered names / footer."""
    events: list[dict] = []
    names: list[str] = []
    footer = ""
    for line in lines:
        if line.startswith("EVT|"):
            parts = line.split("|", 3)
            events.append({
                "turn": parts[1] if len(parts) > 1 else "",
                "name": parts[2] if len(parts) > 2 else "",
                "detail": parts[3] if len(parts) > 3 else "",
            })
        elif line.startswith("EVNAME|"):
            names.append(line[7:])
        elif line.startswith("WR_OK|"):
            footer = line[6:]
    return {"events": events, "event_names": names, "footer": footer}
