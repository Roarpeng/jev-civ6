"""Lightweight HTTP API for the web dashboard.

Provides read-only JSON endpoints for game state plus a whitelisted action
endpoint for bridge clients (e.g. the Jev war-room autopilot, which drives
the game through this API instead of holding its own FireTuner link).
Runs embedded inside the MCP server process, sharing the same
GameConnection via create_app().
"""

import dataclasses
import logging
import traceback

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from civ_mcp import lua as lq
from civ_mcp.connection import LuaError
from civ_mcp.game_state import GameState, _strip_trailing_sentinel

log = logging.getLogger(__name__)

# Whitelisted GameState methods the action endpoint will run. Args come from
# the JSON body as {tool: "...", args: {...}} — keep this list intentional.
ACTION_TOOLS = {
    "end_turn": ("fast",),
    "set_research": ("tech_name",),
    "set_civic": ("civic_name",),
    "set_city_production": (
        "city_id", "item_type", "item_name", "target_x", "target_y",
    ),
    "dismiss_popup": (),
    "move_unit": ("unit_index", "target_x", "target_y"),
    "move_units_batch": ("moves",),
    "fortify_units": ("unit_indexes",),
    "attack_unit": ("unit_index", "target_x", "target_y"),
    "fortify_unit": ("unit_index",),
    "skip_unit": ("unit_index",),
    "skip_remaining_units": (),
    "automate_explore": ("unit_index",),
    "heal_unit": ("unit_index",),
    "set_city_focus": ("city_id", "focus"),
    "list_city_production": ("city_id",),
    "get_threat_scan": (),
    "get_settle_advisor": ("unit_index",),
    "get_settle_candidates": ("unit_index",),
    "found_city": ("unit_index",),
    "alert_unit": ("unit_index",),
    "improve_tile": ("unit_index", "improvement_name"),
    "get_world_congress": (),
    "get_policies": (),
    "set_policies": ("assignments",),
    "queue_wc_votes": ("votes",),
    "submit_congress": (),
    "vote_world_congress": (
        "resolution_hash", "option", "target_index", "num_votes",
    ),
    "get_pending_deals": (),
    "get_city_states": (),
    "send_envoy": ("city_state_player_id",),
    "get_dedications": (),
    "choose_dedication": ("dedication_index",),
    "get_governors": (),
    "appoint_governor": ("governor_type",),
    "assign_governor": ("governor_type", "city_id"),
    "promote_governor": ("governor_type", "promotion_type"),
    "get_great_people": (),
    "activate_great_person": ("unit_index",),
    "purchase_item": ("city_id", "item_type", "item_name", "yield_type"),
    "spread_religion": ("unit_index",),
    "get_religion_founding_status": (),
    "found_religion": ("religion_type", "follower_belief", "founder_belief"),
    "choose_pantheon": ("belief_type",),
    "get_pantheon_status": (),
    "recruit_great_person": ("individual_id",),
    "patronize_great_person": ("individual_id", "yield_type"),
    "reject_great_person": ("individual_id",),
    "respond_to_deal": ("other_player_id", "accept"),
    "get_diplomacy_sessions": (),
    "diplomacy_respond": ("other_player_id", "response"),
    "send_diplomatic_action": ("other_player_id", "action"),
}


def create_app(gs: GameState) -> FastAPI:
    """Create a FastAPI app wired to the given GameState."""
    app = FastAPI(
        title="civ6-mcp API",
        description="Read-only game state API for the Civ 6 web dashboard",
    )
    app.state.gs = gs

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3001"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.exception_handler(ConnectionError)
    async def connection_error_handler(request, exc):
        return JSONResponse(
            status_code=503,
            content={"error": "Game not connected", "detail": str(exc)},
        )

    @app.exception_handler(LuaError)
    async def lua_error_handler(request, exc):
        return JSONResponse(
            status_code=502,
            content={"error": "Lua error", "detail": str(exc)},
        )

    @app.get("/api/overview")
    async def overview(request: Request):
        ov = await request.app.state.gs.get_game_overview()
        return _to_dict(ov)

    @app.get("/api/units")
    async def units(request: Request):
        data = await request.app.state.gs.get_units()
        return _to_dict(data)

    @app.get("/api/cities")
    async def cities(request: Request):
        data = await request.app.state.gs.get_cities()
        return _to_dict(data)

    @app.get("/api/map")
    async def map_area(
        request: Request,
        x: int = Query(..., description="Center X coordinate"),
        y: int = Query(..., description="Center Y coordinate"),
        radius: int = Query(3, ge=1, le=5, description="Radius (1-5)"),
    ):
        tiles = await request.app.state.gs.get_map_area(x, y, radius)
        return _to_dict(tiles)

    @app.get("/api/resources")
    async def resources(request: Request):
        (
            stockpiles,
            owned,
            nearby,
            luxury_count,
        ) = await request.app.state.gs.get_empire_resources()
        return {
            "stockpiles": _to_dict(stockpiles),
            "owned": _to_dict(owned),
            "nearby": _to_dict(nearby),
            "luxury_count": luxury_count,
        }

    @app.get("/api/tech")
    async def tech(request: Request):
        data = await request.app.state.gs.get_tech_civics()
        return _to_dict(data)

    @app.get("/api/diplomacy")
    async def diplomacy(request: Request):
        data = await request.app.state.gs.get_diplomacy()
        return _to_dict(data)

    @app.get("/api/threats")
    async def threats(request: Request):
        data = await request.app.state.gs.get_threat_scan()
        return _to_dict(data)

    @app.get("/api/warroom_collect")
    async def warroom_collect(request: Request):
        """The war-room's whole per-turn collect in TWO FireTuner roundtrips
        (InGame: overview+units+cities; GameCore: tech+threats) instead of
        five serialized ones. Sections are delimited by SECTION| markers and
        fed to the same parsers the individual endpoints use."""
        gs: GameState = request.app.state.gs

        def _sections(lines: list[str]) -> dict[str, list[str]]:
            out: dict[str, list[str]] = {}
            cur = "_pre"
            for line in lines:
                if line.startswith("SECTION|"):
                    cur = line.split("|", 1)[1]
                    out.setdefault(cur, [])
                else:
                    out.setdefault(cur, []).append(line)
            return out

        # ONE roundtrip: the GameCore reads (tech/threats) run fine in the
        # InGame context too, so everything merges into a single script.
        # Low-frequency sections (policies/city-states/governors) are only
        # included when the caller asks (?pol=1&cs=1&gov=1).
        want_pol = request.query_params.get("pol") == "1"
        want_cs = request.query_params.get("cs") == "1"
        want_gov = request.query_params.get("gov") == "1"
        try:
            lua = (
                'print("SECTION|wr"); '
                + _strip_trailing_sentinel(lq.build_wr_drain())
                + ' print("SECTION|overview"); '
                + _strip_trailing_sentinel(lq.build_overview_query())
                + ' print("SECTION|units"); '
                + _strip_trailing_sentinel(lq.build_units_query())
                + ' print("SECTION|cities"); '
                + _strip_trailing_sentinel(lq.build_cities_query())
            )
            # NOTE: tech/threats must run in the GameCore context — some of
            # their APIs are nil in InGame (empirically: LuaError at merge)
            gc_lua = (
                'print("SECTION|tech"); '
                + _strip_trailing_sentinel(lq.build_tech_civics_query())
                + ' print("SECTION|threats"); '
                + lq.build_threat_scan_query()
            )
            if want_pol:
                lua += (' print("SECTION|policies"); '
                        + _strip_trailing_sentinel(lq.build_policies_query()))
            if want_cs:
                lua += (' print("SECTION|cs"); '
                        + _strip_trailing_sentinel(lq.build_city_states_query()))
            if want_gov:
                lua += (' print("SECTION|gov"); '
                        + lq.build_governors_query())
            w_sections = _sections(await gs.conn.execute_write(lua))
            r_sections = _sections(await gs.conn.execute_read(gc_lua))

            wr = lq.parse_wr_lines(w_sections.get("wr", []))
            gov = (lq.parse_policies_response(w_sections.get("policies", []))
                   if want_pol else None)
            cs = (lq.parse_city_states_response(w_sections.get("cs", []))
                  if want_cs else None)
            govst = (lq.parse_governors_response(w_sections.get("gov", []))
                     if want_gov else None)
        except Exception as e:  # noqa: BLE001 — surface the cause to callers
            return JSONResponse(
                status_code=500,
                content={"error": "collect failed",
                         "detail": f"{type(e).__name__}: {e}",
                         "tb": traceback.format_exc()[-1500:]},
            )

        ov = lq.parse_overview_response(w_sections.get("overview", []))
        units = lq.parse_units_response(w_sections.get("units", []))
        cities, distances = lq.parse_cities_response(w_sections.get("cities", []))
        tech = lq.parse_tech_civics_response(r_sections.get("tech", []))
        threats = lq.parse_threat_scan_response(r_sections.get("threats", []))
        return {
            "overview": _to_dict(ov),
            "units": _to_dict(units),
            "cities": [_to_dict(cities), distances],
            "tech": _to_dict(tech),
            "threats": _to_dict(threats),
            "policies": _to_dict(gov),
            "city_states": _to_dict(cs),
            "governors": _to_dict(govst),
            "wr": wr,
        }

    @app.get("/api/turnstate")
    async def turnstate(request: Request):
        """Cheap GameCore-only probe for continuous monitoring — safe to call
        while AI civs process their turns (no InGame context switch)."""
        data = await request.app.state.gs.get_quick_state()
        return _to_dict(data)

    @app.get("/api/settle_candidates")
    async def settle_candidates(request: Request, unit_index: int):
        data = await request.app.state.gs.get_settle_candidates(unit_index)
        return _to_dict(data)

    @app.get("/api/district_advisor")
    async def district_advisor(request: Request, city_id: int, district_type: str):
        data = await request.app.state.gs.get_district_advisor(city_id, district_type)
        return _to_dict(data)

    class ActionRequest(BaseModel):
        tool: str
        args: dict = {}

    @app.post("/api/action")
    async def action(req: ActionRequest, request: Request):
        """Run a whitelisted GameState method. Bridge clients use this to
        drive the game without owning a FireTuner connection."""
        if req.tool not in ACTION_TOOLS:
            raise HTTPException(
                status_code=422,
                detail=f"unknown tool {req.tool!r}; allowed: {sorted(ACTION_TOOLS)}",
            )
        gs: GameState = request.app.state.gs
        kwargs = {k: v for k, v in req.args.items()
                  if v is not None and k in ACTION_TOOLS[req.tool]}
        try:
            result = await getattr(gs, req.tool)(**kwargs)
        except TypeError as e:
            raise HTTPException(status_code=422, detail=f"bad args: {e}") from e
        # end_turn & friends return narration strings; queries return dataclasses
        if isinstance(result, str):
            return {"result": result}
        return {"data": _to_dict(result)}

    return app


def _to_dict(obj):
    """Serialize dataclass instances (including nested) to plain dicts."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj
