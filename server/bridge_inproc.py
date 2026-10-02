# -*- coding: utf-8 -*-
"""InProcessBridge: the civ6-mcp link without the subprocess + HTTP hop.

Why this refactor: the old ``Bridge`` (server/autopilot.py) spawned
``python -m civ_mcp`` as a child process and drove it through the embedded
web API on localhost:8000. That extra process bought isolation but created a
whole failure class that had nothing to do with the game — spawn failures
(wrong interpreter / missing PYTHONPATH), readiness races at startup, port
quota/exhaustion, HTTP timeouts masquerading as game errors, and orphaned
bridge processes surviving the autopilot that spawned them.

InProcessBridge runs the SAME battle-tested library directly:
``civ_mcp.connection.GameConnection`` + ``civ_mcp.game_state.GameState``
and the exact Lua corpus behind them. A dedicated daemon thread with its
own asyncio event loop OWNS the connection and game state; every public
method is synchronous and dispatches onto that loop via
``asyncio.run_coroutine_threadsafe(...).result(timeout)`` — the connection
is never created lazily on caller threads.

The public surface mirrors the old Bridge method-for-method:

* reads (overview/units/cities/tech/threats/diplomacy/turnstate/
  warroom_collect/map_area/settle_candidates/district_advisor) call the
  SAME GameState methods the web endpoints called and return the SAME
  shapes (dataclasses dictified exactly like web_api's ``_to_dict``);
  failures surface as ``{"error": ...}`` dicts, like HTTP errors did.
* ``act`` / ``act_data`` keep the semantics of POST /api/action: string
  narrations come back as ``{"result": str}`` (returned as ``str``),
  structured results as ``{"data": obj}`` (returned as parsed dict/list),
  exceptions as ``"Error: <msg>"`` strings — the autopilot's error path
  keeps working unchanged.
* ``ACTION_TOOLS`` below is copied verbatim from
  civ6-mcp/src/civ_mcp/web_api.py — it is the compatibility contract.

Importing this module requires no game (and not even civ_mcp): the real
factories resolve their imports lazily, and tests inject fakes through the
``gs_factory`` / ``conn_factory`` seams.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import logging
import threading
import time

from . import config as cfgmod

log = logging.getLogger("jevciv6.bridge")

# Whitelisted GameState methods the action dispatcher will run, copied
# VERBATIM from civ6-mcp/src/civ_mcp/web_api.py — the compatibility
# contract between the autopilot and the game library. Args come in as
# {tool: "...", args: {...}}; only whitelisted keys are forwarded.
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
    "get_trade_routes": (),
    "get_trade_destinations": ("unit_index",),
    "make_trade_route": ("unit_index", "target_x", "target_y"),
    "get_unit_promotions": ("unit_id",),
    "promote_unit": ("unit_id", "promotion_type"),
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
    "religious_units_batch": ("actions",),
    "orders_batch": ("orders",),
    "list_saves": (),
    "load_game_save": ("save_name",),
    "load_save_menu": ("save_name",),
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

READ_TIMEOUT_S = 60.0    # matches the old HTTP default timeout
HEALTH_TIMEOUT_S = 8.0   # matches the old healthy() probe timeout
STOP_TIMEOUT_S = 8.0     # matches the old terminate/kill grace


def _make_actor(method_name: str):
    """async callable(gs, **kwargs) — the GameState method behind a tool."""
    async def _actor(gs, **kwargs):
        return await getattr(gs, method_name)(**kwargs)
    return _actor


_ACTORS = {name: _make_actor(name) for name in ACTION_TOOLS}


def _to_dict(obj):
    """Serialize dataclass instances (including nested) to plain dicts —
    identical to web_api._to_dict so response shapes match the HTTP API."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, (list, tuple)):
        return [_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


def _split_sections(lines: list[str]) -> dict[str, list[str]]:
    """Split a concatenated multi-section Lua output on SECTION| markers
    (same helper web_api's /api/warroom_collect endpoint uses)."""
    out: dict[str, list[str]] = {}
    cur = "_pre"
    for line in lines:
        if line.startswith("SECTION|"):
            cur = line.split("|", 1)[1]
            out.setdefault(cur, [])
        else:
            out.setdefault(cur, []).append(line)
    return out


def _default_gs_factory(conn):
    """Real GameState factory (lazy import: no civ_mcp needed to import
    this module, and never a game)."""
    from civ_mcp.game_state import GameState
    return GameState(conn)


def _make_real_conn_factory(cfg):
    def _factory():
        from civ_mcp.connection import GameConnection
        return GameConnection(host=cfg.bridge.game_host,
                              port=int(cfg.bridge.game_port))
    return _factory


async def _ensure_states(conn) -> None:
    """Await the connection's game-state discovery. The real GameConnection
    spells it ``_ensure_game_states``; tolerate the public spelling too."""
    # No-op at connect time: at the main menu InGame/GameCore states don't
    # exist yet and the civ_mcp helper would reconnect-loop until start()
    # timed out. _ensure() re-discovers after the operator loads a save.
    return


class InProcessBridge:
    """Owns the civ6-mcp GameConnection + GameState inside a dedicated
    daemon thread's event loop — the subprocess/HTTP Bridge, minus the
    subprocess and the HTTP."""

    def __init__(self, journal, cfg=None, gs_factory=None,
                 conn_factory=None, takeover_fn=None):
        self.journal = journal          # may be None (tests)
        self.cfg = cfg or cfgmod.load()
        self.gs_factory = gs_factory or _default_gs_factory
        self.conn_factory = conn_factory or _make_real_conn_factory(self.cfg)
        # Optional (cfg) -> str probe run before connecting. The autopilot's
        # _takeover_once is NOT imported here to avoid a circular import
        # (autopilot will import this module); pass it in from the caller.
        self.takeover_fn = takeover_fn
        self._ready_timeout_s = float(
            getattr(self.cfg.bridge, "ready_timeout_s", 90.0) or 90.0)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conn = None   # owned by the loop thread only
        self._gs = None     # owned by the loop thread only

    # ── journal seam (journal may be None) ────────────────────────

    def _journal(self, kind: str, payload: dict, turn=None) -> None:
        if self.journal is None:
            return
        try:
            self.journal.add(kind, payload, turn=turn)
        except Exception:  # noqa: BLE001 — never break gameplay on logging
            log.debug("journal write failed", exc_info=True)

    # ── loop-thread lifecycle ─────────────────────────────────────

    def _start_loop_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="jevciv6-bridge-loop", daemon=True)
        self._thread.start()

    def _teardown_loop(self, join_s: float = 5.0) -> None:
        loop, thread = self._loop, self._thread
        self._loop = self._thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:  # already stopped/closed
                pass
        if thread is not None:
            thread.join(join_s)
        if loop is not None:
            try:
                loop.close()
            except RuntimeError:
                pass

    def _run_coro(self, coro, timeout: float):
        """Schedule coro on the owned loop; block the caller for the
        result. Raises RuntimeError when the loop thread is not running."""
        if (self._loop is None or self._thread is None
                or not self._thread.is_alive()):
            coro.close()  # never scheduled — silence "never awaited"
            raise RuntimeError(
                "in-process bridge is not running (call start() first)")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError as e:
            fut.cancel()
            raise TimeoutError(
                f"bridge call timed out after {timeout}s") from e

    # ── lifecycle (async, like the old Bridge) ────────────────────

    async def start(self, turn=None) -> None:
        if self.takeover_fn is not None:
            report = await asyncio.to_thread(self.takeover_fn, self.cfg)
            self._journal("action", {
                "tool": "bridge_takeover", "args": {}, "result": report,
            }, turn=turn)
        self._start_loop_thread()
        deadline = time.monotonic() + self._ready_timeout_s
        while True:
            fut = asyncio.run_coroutine_threadsafe(
                self._connect_once(), self._loop)
            try:
                summary = await asyncio.wait_for(
                    asyncio.wrap_future(fut),
                    timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                fut.cancel()
                self._teardown_loop()
                raise RuntimeError(
                    f"bridge not ready after {self._ready_timeout_s}s")
            except Exception as e:  # noqa: BLE001
                if time.monotonic() >= deadline:
                    self._teardown_loop()
                    raise RuntimeError(
                        f"in-process bridge failed to start: {e}") from e
                await asyncio.sleep(2)  # retry cadence of the old spawn poll
                continue
            self._journal("action", {
                "tool": "bridge_up", "args": {},
                "result": f"in-process bridge ready — {summary}",
            }, turn=turn)
            return

    async def stop(self, turn=None) -> None:
        conn, loop = self._conn, self._loop
        was_up = loop is not None or conn is not None
        if (conn is not None and loop is not None and self._thread is not None
                and self._thread.is_alive()):
            fut = asyncio.run_coroutine_threadsafe(self._disconnect(conn), loop)
            try:
                await asyncio.to_thread(fut.result, STOP_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — stop must always succeed
                log.debug("bridge disconnect failed", exc_info=True)
        self._conn = None
        self._gs = None
        self._teardown_loop()
        if was_up:
            self._journal("action", {
                "tool": "bridge_down", "args": {},
                "result": "in-process bridge stopped — FireTuner slot "
                          "released",
            }, turn=turn)

    @staticmethod
    async def _disconnect(conn) -> None:
        try:
            await conn.disconnect()
        except Exception:  # noqa: BLE001
            log.debug("bridge disconnect raised", exc_info=True)

    @property
    def proc(self):
        """Legacy subprocess shim: autopilot status reporting reads .proc.pid."""
        import os
        from types import SimpleNamespace
        return SimpleNamespace(pid=os.getpid())

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def menu_ping(self) -> bool:
        """Menu-safe liveness+rediscovery check. Executes a trivial print in
        the frontend Main State (index 0) directly — never routes through
        _ensure(), whose game-state rediscovery would reconnect-loop and
        hammer the tuner while we sit at the main menu. Returns True when
        game states have appeared (a save finished loading)."""
        if not self.alive():
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._menu_ping(), self._loop)
            return bool(fut.result(HEALTH_TIMEOUT_S))
        except Exception:  # noqa: BLE001
            return False

    async def _menu_ping(self) -> bool:
        conn = self._conn
        if conn is None:
            return False
        if not conn.is_connected:
            try:
                await conn.connect()
            except Exception:  # noqa: BLE001
                return False
        try:
            await conn.execute_in_state(0, 'print("PING")', timeout=5.0)
        except Exception:  # noqa: BLE001
            return False
        return self.game_loaded()

    def game_loaded(self) -> bool:
        """True once InGame/GameCore states were discovered (a save is
        loaded). False while sitting at the main menu."""
        conn = self._conn
        return bool(conn is not None and (
            getattr(conn, "ingame_index", None) is not None
            or getattr(conn, "gamecore_index", None) is not None))

    def healthy(self) -> bool:
        """True when the link is up and a game context was discovered."""
        if not self.alive():
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._health_probe(), self._loop)
            return bool(fut.result(HEALTH_TIMEOUT_S))
        except Exception:  # noqa: BLE001
            return False

    async def _health_probe(self) -> bool:
        conn = self._conn
        if conn is None or not conn.is_connected:
            return False
        return (getattr(conn, "ingame_index", None) is not None
                or getattr(conn, "gamecore_index", None) is not None)

    # ── connection management (loop thread only) ──────────────────

    async def _connect_once(self) -> str:
        conn = self.conn_factory()
        try:
            await conn.connect()
            await _ensure_states(conn)
            gs = self.gs_factory(conn)
        except Exception:
            # release the single-client FireTuner slot so the retry can
            # claim it cleanly
            self._conn = None
            self._gs = None
            try:
                await conn.disconnect()
            except Exception:  # noqa: BLE001 — cleanup must not mask the cause
                log.debug("post-failure disconnect failed", exc_info=True)
            raise
        self._conn = conn
        self._gs = gs
        return (f"lua_states={len(getattr(conn, 'lua_states', None) or {})} "
                f"gamecore={conn.gamecore_index} ingame={conn.ingame_index}")

    async def _ensure(self) -> None:
        """Run before every dispatched call: reconnect if the single-client
        FireTuner link dropped (mid-call errors surface as 'Error: ...'
        strings — the autopilot's error path handles respawning)."""
        conn = self._conn
        if conn is not None and conn.is_connected:
            return
        log.info("in-process bridge link lost — reconnecting")
        await self._connect_once()

    # ── action dispatch (POST /api/action equivalent) ─────────────

    async def _dispatch(self, tool: str, args: dict):
        await self._ensure()
        if tool not in ACTION_TOOLS:
            raise ValueError(
                f"unknown tool {tool!r}; allowed: {sorted(ACTION_TOOLS)}")
        # mirror the endpoint's arg filtering: only whitelisted, non-None
        # keys are forwarded (e.g. end_turn's "fast", move_units_batch's
        # "moves"; set_policies' int-key coercion lives in GameState)
        kwargs = {k: v for k, v in (args or {}).items()
                  if v is not None and k in ACTION_TOOLS[tool]}
        return await _ACTORS[tool](self._gs, **kwargs)

    def act(self, tool: str, args: dict, timeout: float = 60.0) -> str:
        """String result like the old action endpoint's "result" field."""
        try:
            result = self._run_coro(self._dispatch(tool, args), timeout)
        except Exception as e:  # noqa: BLE001 — callers match on "Error:"
            return f"Error: {e}"
        if isinstance(result, str):
            return result
        return str(_to_dict(result))

    def act_data(self, tool: str, args: dict, timeout: float = 60.0):
        """Structured payload like the old {"data": ...} responses."""
        try:
            result = self._run_coro(self._dispatch(tool, args), timeout)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        data = _to_dict(result)
        return data if isinstance(data, (list, dict)) else []

    # ── typed convenience wrappers (GET endpoints) ────────────────

    def _read(self, coro, timeout: float = READ_TIMEOUT_S):
        try:
            return self._run_coro(coro, timeout)
        except Exception as e:  # noqa: BLE001 — reads surface error dicts
            return {"error": str(e)}

    async def _read_call(self, method: str, *method_args):
        await self._ensure()
        return _to_dict(await getattr(self._gs, method)(*method_args))

    def overview(self) -> dict:
        return self._read(self._read_call("get_game_overview"))

    def units(self) -> list:
        return self._read(self._read_call("get_units"))

    def cities(self) -> dict:
        return self._read(self._read_call("get_cities"))  # [cities, distances]

    def tech(self) -> dict:
        return self._read(self._read_call("get_tech_civics"))

    def threats(self) -> list:
        return self._read(self._read_call("get_threat_scan"))

    def diplomacy(self) -> dict:
        return self._read(self._read_call("get_diplomacy"))

    def turnstate(self) -> dict:
        return self._read(self._read_call("get_quick_state"))

    def map_area(self, x: int, y: int, radius: int = 2) -> list:
        return self._read(self._read_call("get_map_area", x, y, radius))

    def settle_candidates(self, unit_index: int) -> list:
        return self._read(self._read_call("get_settle_candidates", unit_index))

    def district_advisor(self, city_id: int, district_type: str) -> list:
        return self._read(
            self._read_call("get_district_advisor", city_id, district_type))

    def production_options(self, city_id: int) -> list:
        data = self.act_data("list_city_production", {"city_id": city_id})
        return data if isinstance(data, list) else []

    def warroom_collect(self, pol: bool = False, cs: bool = False,
                        gov: bool = False,
                        prod_city_ids: list | None = None) -> dict:
        """Whole per-turn collect in ONE batched Lua roundtrip; low-frequency
        sections (policies/city-states/governors) only when flagged."""
        return self._read(
            self._warroom_collect(pol, cs, gov, prod_city_ids))

    async def _warroom_collect(self, want_pol: bool, want_cs: bool,
                               want_gov: bool,
                               prod_city_ids: list | None = None) -> dict:
        """Body of web_api's /api/warroom_collect: two batched roundtrips
        (InGame write + GameCore read) with SECTION|-delimited scripts,
        parsed by the same civ_mcp.lua parsers the endpoint used."""
        from civ_mcp import lua as lq
        from civ_mcp.game_state import _strip_trailing_sentinel
        await self._ensure()
        conn = self._gs.conn

        # ONE roundtrip: the GameCore reads (tech/threats) run fine in the
        # InGame context too, so everything merges into a single script.
        def _sec(name: str, body: str) -> str:
            return (f'print("SECT|{name}|0"); local _c0=os.clock() '
                    + body
                    + f' print("SECT|{name}|" .. math.floor((os.clock()-_c0)*1000)) ')

        lua = (
            _sec("wr", 'print("SECTION|wr"); '
                 + _strip_trailing_sentinel(lq.build_wr_drain()))
            + _sec("overview", 'print("SECTION|overview"); '
                   + _strip_trailing_sentinel(lq.build_overview_query()))
            + _sec("units", 'print("SECTION|units"); '
                   + _strip_trailing_sentinel(lq.build_units_query()))
            + _sec("cities", 'print("SECTION|cities"); '
                   + _strip_trailing_sentinel(lq.build_cities_query()))
        )
        # NOTE: tech/threats must run in the GameCore context — some of
        # their APIs are nil in InGame (empirically: LuaError at merge)
        gc_lua = (
            'print("SECTION|tech"); '
            + _strip_trailing_sentinel(lq.build_tech_civics_query())
            + ' print("SECTION|threats"); '
            + lq.build_threat_scan_query()
            + ' print("SECTION|promos"); '
            + lq.build_gc_promo_scan()
        )
        if want_pol:
            lua += (' print("SECTION|policies"); '
                    + _strip_trailing_sentinel(lq.build_policies_query()))
        if want_cs:
            lua += (' print("SECTION|cs"); '
                    + _strip_trailing_sentinel(lq.build_city_states_query()))
        if want_gov:
            lua += ' print("SECTION|gov"); ' + lq.build_governors_query()
        # reflex probes: pantheon flag + production options for KNOWN idle
        # cities (ids passed from the previous snapshot — batches up to 3
        # separate roundtrips into this one script)
        lua += ' print("SECTION|panflag"); ' + lq.build_wr_pantheon_flag()
        for cid in (prod_city_ids or [])[:3]:
            try:
                lua += (f' print("SECTION|prod_{int(cid)}"); '
                        + _strip_trailing_sentinel(
                            lq.build_city_production_query(int(cid))))
            except Exception:  # noqa: BLE001 — one bad id must not kill collect
                pass

        import time as _t
        _t0 = _t.monotonic()
        w_lines = await conn.execute_write(lua)
        _t1 = _t.monotonic()
        r_lines = await conn.execute_read(gc_lua)
        _t2 = _t.monotonic()
        _sects = {}
        for ln in w_lines:
            if ln.startswith("SECT|"):
                _, nm, ms = ln.split("|", 2)
                if ms.isdigit() and int(ms) > 0:
                    _sects.setdefault(nm, []).append(int(ms))
        _sec_summary = ",".join(
            f"{nm}:{max(v)}" for nm, v in
            sorted(_sects.items(), key=lambda kv: -max(kv[1]))[:4] if v)
        _leg_timing = {"ingame": round(_t1 - _t0, 2),
                       "gamecore": round(_t2 - _t1, 2),
                       "sections": _sec_summary or "-"}
        w_sections = _split_sections(w_lines)
        r_sections = _split_sections(r_lines)

        wr = lq.parse_wr_lines(w_sections.get("wr", []))
        pol_data = (lq.parse_policies_response(w_sections.get("policies", []))
                    if want_pol else None)
        cs_data = (lq.parse_city_states_response(w_sections.get("cs", []))
                   if want_cs else None)
        gov_data = (lq.parse_governors_response(w_sections.get("gov", []))
                    if want_gov else None)
        ov = lq.parse_overview_response(w_sections.get("overview", []))
        units = lq.parse_units_response(w_sections.get("units", []))
        cities, distances = lq.parse_cities_response(
            w_sections.get("cities", []))
        tech = lq.parse_tech_civics_response(r_sections.get("tech", []))
        threats = lq.parse_threat_scan_response(r_sections.get("threats", []))
        # reflex probes (folded into the two collect legs — zero extra
        # roundtrips vs the old per-unit/per-city query chains)
        pan_flag = None
        for ln in w_sections.get("panflag", []):
            if ln.startswith("PANFLAG|"):
                parts = ln.split("|")
                pan_flag = {"has_pantheon": parts[1] == "1",
                            "faith": float(parts[2]) if len(parts) > 2 else 0.0}
                break
        promos = []
        for ln in r_sections.get("promos", []):
            if ln.startswith("PROMO|"):
                parts = ln.split("|")
                if len(parts) >= 3:
                    promos.append({"unit_id": int(parts[1]),
                                   "promotion_type": parts[2]})
        prod_by_city = {}
        for key, lines_ in w_sections.items():
            if key.startswith("prod_"):
                try:
                    cid = int(key[5:])
                except ValueError:
                    continue
                opts = lq.parse_city_production_response(lines_)
                prod_by_city[cid] = _to_dict(opts)
        return {
            "_leg_timing": _leg_timing,
            "pantheon_flag": pan_flag,
            "promos": promos,
            "prod_by_city": prod_by_city,
            "overview": _to_dict(ov),
            "units": _to_dict(units),
            "cities": [_to_dict(cities), distances],
            "tech": _to_dict(tech),
            "threats": _to_dict(threats),
            "policies": _to_dict(pol_data),
            "city_states": _to_dict(cs_data),
            "governors": _to_dict(gov_data),
            "wr": wr,
        }
