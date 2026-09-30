# -*- coding: utf-8 -*-
"""Autopilot: the LLM-driven play loop, bridged.

Architecture (the bridge insight): Civ 6's FireTuner link is single-client
and temperamental about reconnects. The civ6-mcp server (upstream, battle-
tested) is the ONE process that owns that link — its embedded web API on
port 8000 exposes reads and a whitelisted /api/action endpoint. The
autopilot never touches FireTuner itself:

    auto mode → [one-time takeover] → spawn `python -m civ_mcp` (bridge)
              → cycle: HTTP reads → decision gate → LLM → HTTP actions
    manual mode → bridge process dies with the loop

Monitored turn cycle (v2)
-------------------------
1. decide cycle: collect → gate → (LLM judgment at decision points) → execute
2. fire end_turn with a bounded HTTP timeout, then keep polling the cheap
   GameCore-only /api/turnstate (≈1 s cadence — safe while the AI processes
   its turn) until the turn advances → next cycle starts immediately
3. end_turn "blockers" (World Congress / trade deals / diplomacy / stuck
   units / hangs) are classified and handled (or the pilot pauses loudly,)
   instead of hammering end_turn forever
4. a stall watchdog pauses the pilot if a turn makes no progress for
   stall_limit_s — the 11-hour spin of campaign #1 can never repeat

Timing knobs live in server/config.py (jevciv6.toml).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from . import config as cfgmod

log = logging.getLogger("jevciv6.autopilot")

DEFAULT_FAIL_LIMIT = 3
BRIDGE_URL = "http://127.0.0.1:8000"   # overridden at runtime via set_bridge_url
BLOCKER_PER_KIND_CAP = 2               # same blocker handled N times → pause
BLOCKER_TOTAL_CAP = 6                  # total blocker handling per advance

HANDLABLE_BLOCKERS = ("world_congress", "trade_deal", "diplomacy", "blockers", "hang")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_bridge_url(url: str) -> None:
    global BRIDGE_URL
    BRIDGE_URL = url or BRIDGE_URL


def _http(method: str, path: str, body: dict | None = None, timeout: float = 60.0):
    url = BRIDGE_URL + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}", "detail": e.read().decode()[:200]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def _quick_state() -> dict | None:
    """Cheap GameCore-only probe (safe during AI turn processing)."""
    r = _http("GET", "/api/turnstate", timeout=5.0)
    if isinstance(r, dict) and "error" not in r and "turn" in r:
        return r
    return None


def _takeover_once(cfg) -> str:
    """Kill any other python process holding the FireTuner link, so the
    bridge we are about to spawn can claim it. Excludes our own process.
    Windows-only; skipped elsewhere or when disabled in config."""
    if platform.system() != "Windows" or not cfg.autopilot.takeover_on_start:
        return "skipped (takeover disabled or non-Windows)"
    port = int(cfg.bridge.game_port)
    ps = ("Get-NetTCPConnection -RemotePort %d -State Established "
          "-ErrorAction SilentlyContinue | "
          "Select-Object -ExpandProperty OwningProcess -Unique" % port)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=15).stdout
    victims = [p.strip() for p in out.splitlines()
               if p.strip().isdigit() and int(p.strip()) != os.getpid()]
    if not victims:
        return "no competing controller"
    reports = []
    for pid in victims:
        info = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15).stdout
        image = info.split('","')[0].strip('"') if '","' in info else "?"
        if "python" not in image.lower():
            reports.append(f"skip PID {pid} ({image})")
            continue
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        reports.append(f"killed PID {pid} ({image})")
    return "; ".join(reports)


# ────────────────────────────── end_turn classification ────────────────────

def parse_turn_advance(result: str, before) -> int | None:
    """Extract the new turn number from an end_turn narration, if it advanced."""
    r = str(result or "")
    m = re.search(r"Turn\s+(\d+)\s*->\s*(\d+)", r)
    if m:
        new = int(m.group(2))
        if before is None or new > int(before):
            return new
    m = re.search(r"advanced to (\d+)", r)
    if m:
        new = int(m.group(1))
        if before is None or new > int(before):
            return new
    return None


def classify_end_turn(result: str) -> str:
    """Bucket the bridge's end_turn narration into actionable categories:
    ok | game_over | hang | world_congress | trade_deal | diplomacy |
    blockers | timeout | error."""
    r = str(result or "")
    if "GAME OVER" in r:
        return "game_over"
    if r.startswith("HANG:") or "\nHANG:" in r:
        return "hang"
    if "World Congress fires" in r:
        return "world_congress"
    low = r.lower()
    if "trade deal" in low or "deal pending" in low:
        return "trade_deal"
    if "diplomacy" in low and ("pending" in low or "cannot end turn" in low):
        return "diplomacy"
    if "Cannot end turn" in r or "End turn blocked" in r:
        return "blockers"
    if "timed out" in r:
        return "timeout"
    if r.startswith("Error"):
        return "error"
    return "ok"


def _hex_distance(x1: int, y1: int, x2: int, y2: int) -> int:
    """Cube-distance for Civ 6's offset hex grid (odd-r style approximation —
    only used for picking which units to move; the engine validates moves)."""
    def to_cube(x: int, y: int):
        q = x - (y - (y & 1)) // 2
        return q, y
    q1, r1 = to_cube(x1, y1)
    q2, r2 = to_cube(x2, y2)
    return (abs(q1 - q2) + abs(q1 + r1 - q2 - r2) + abs(r1 - r2)) // 2


# ────────────────────────────── snapshot builders ───────────────────────────

def _tech_id(t: dict) -> str:
    name = t.get("name", "")
    if name.startswith("TECH_"):
        return name
    return (t.get("tech_type") or name).replace("TECHNOLOGY_", "TECH_")


def _civic_id(c: dict) -> str:
    name = c.get("name", "")
    if name.startswith("CIVIC_"):
        return name
    return (c.get("civic_type") or name).replace("CIVICS_", "CIVIC_")


def _is_building_idle(currently_building) -> bool:
    """The game reports an empty build queue as 'nothing' (lowercase), 'NONE'
    or None depending on version — treat all of them as idle."""
    return str(currently_building or "").strip().lower() in ("", "none", "nothing")


def _settle_desc(c: dict) -> str:
    res = ", ".join(str(r) for r in (c.get("resources") or [])) or "no resources"
    try:
        score = float(c.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return (f"score {score:.1f} · food {c.get('total_food', 0)} · "
            f"prod {c.get('total_prod', 0)} · {c.get('water_type', '?')} water · "
            f"{res} · defense {c.get('defense_score', 0)}")


def _compact_tiles(tiles: list) -> list:
    out = []
    for t in tiles[:25]:
        if not isinstance(t, dict):
            continue
        item = {"at": [t.get("x"), t.get("y")], "terrain": t.get("terrain")}
        for k in ("feature", "resource", "improvement", "district"):
            if t.get(k):
                item[k] = t[k]
        if t.get("is_river"):
            item["river"] = True
        if t.get("own_units"):
            item["own_units"] = t["own_units"]
        if t.get("units"):
            item["units"] = t["units"]
        out.append(item)
    return out


def build_snapshot(ov: dict, units: list, cities: list, tech: dict,
                   threats: list, prod_by_city: list | None = None,
                   settle_candidates: list | None = None,
                   map_tiles: list | None = None) -> dict:
    idle_settlers = [u for u in units
                     if "SETTLER" in str(u.get("unit_type", "")).upper()
                     and u.get("moves_remaining", 0) > 0]
    return {
        "turn": ov.get("turn"),
        "civ": ov.get("civ_name"),
        "leader": ov.get("leader_name"),
        "difficulty": ov.get("difficulty"),
        "score": ov.get("score"),
        "yields": {
            "gold": ov.get("gold"), "gold_per_turn": ov.get("gold_per_turn"),
            "science": ov.get("science_yield"), "culture": ov.get("culture_yield"),
            "faith": ov.get("faith"),
        },
        "research": {
            "name": tech.get("current_research") or "",
            "turns_left": tech.get("current_research_turns"),  # 0 = just
            # completed → decision gate must see it (never coalesce to None)
        },
        "civic": ({"name": tech.get("current_civic"),
                   "turns_left": tech.get("current_civic_turns")}
                  if tech.get("current_civic") else None),
        "cities": [
            {"city_id": c.get("city_id"), "name": c.get("name"),
             "at": [c.get("x"), c.get("y")],
             "pop": c.get("population"), "growth": f"{c.get('turns_to_grow')}t",
             "food_surplus": c.get("food_surplus"),
             "production": (str(c.get("currently_building"))
                            if not _is_building_idle(c.get("currently_building"))
                            else ""),
             "defense": c.get("defense_strength"),
             "garrison": c.get("garrison_unit"),
             "districts": c.get("districts") or [],
             "unimproved_resources": c.get("unimproved_resources") or []}
            for c in cities
        ],
        "units": [
            {"unit_index": u.get("unit_index"), "type": u.get("unit_type"),
             "at": [u.get("x"), u.get("y")], "cs": u.get("combat_strength"),
             "hp": u.get("health"), "max_hp": u.get("max_health"),
             "moves": u.get("moves_remaining"), "targets": u.get("targets") or []}
            for u in units
        ],
        "threats": [
            {"type": t.get("unit_type"), "at": [t.get("x"), t.get("y")],
             "cs": t.get("combat_strength"), "hp": t.get("hp"),
             "distance": t.get("distance"), "owner": t.get("owner_name")}
            for t in threats
        ],
        "notes": [],
        "available": {
            "techs": [
                {"id": _tech_id(t),
                 "desc": f"{t.get('turns')}t, unlocks: {t.get('unlocks') or '?'}"}
                for t in (tech.get("available_techs") or [])
            ],
            "civics": [
                {"id": _civic_id(c), "desc": f"{c.get('turns')}t"}
                for c in (tech.get("available_civics") or [])
            ],
            "production_by_city": [
                {"city_id": e.get("city_id"), "name": e.get("name"),
                 "options": [
                     {"id": p.get("item_name"),
                      "desc": f"{p.get('category')} · {p.get('turns')}t"}
                     for p in (e.get("options") or [])
                 ]}
                for e in (prod_by_city or [])
            ],
        },
        "settle_candidates": settle_candidates or [],
        "map_near_capital": map_tiles or [],
        "idle_settler_count": len(idle_settlers),
    }


def build_judge_state(snapshot: dict) -> dict:
    """Curate the machine snapshot into the richer state object the judge
    (TypeSafe Jev or any configured LLM) reasons over. Keys here are
    referenced by decision_gate question instructions — keep them in sync."""
    s = snapshot
    yields = s.get("yields") or {}
    res = s.get("research") or {}
    civ = s.get("civic") or {}
    cities = [
        {"name": c.get("name"), "pop": c.get("pop"), "growth": c.get("growth"),
         "production": c.get("production") or "NOTHING (idle!)",
         "defense": c.get("defense"), "garrison": c.get("garrison"),
         "districts": c.get("districts"),
         "unimproved_resources": c.get("unimproved_resources")}
        for c in (s.get("cities") or [])
    ]
    units = [
        {"type": u.get("type"), "at": u.get("at"), "moves": u.get("moves"),
         "hp": (f"{u.get('hp')}/{u.get('max_hp')}"
                if u.get("hp") is not None else None),
         "cs": u.get("cs"),
         "can_attack": u.get("targets") or None}
        for u in (s.get("units") or [])
    ]
    situation = (
        f"Turn {s.get('turn')} | {s.get('civ')} ({s.get('leader')}) | "
        f"difficulty {s.get('difficulty')} | score {s.get('score')} | "
        f"gold {yields.get('gold')} ({yields.get('gold_per_turn')}/t) | "
        f"science {yields.get('science')}/t | culture {yields.get('culture')}/t | "
        f"research: {res.get('name') or 'NONE (idle!)'} | "
        f"civic: {civ.get('name') or 'NONE (idle!)'}"
    )
    return {
        "situation": situation,
        "empire": {"yields": yields, "cities": cities, "units": units},
        "threats": s.get("threats") or [],
        "map_near_capital": s.get("map_near_capital") or None,
        "settle_candidates": s.get("settle_candidates") or None,
        "available": s.get("available"),
        "notes": s.get("notes") or [],
    }


# ────────────────────────────── bridge process ──────────────────────────────

class Bridge:
    """Owns the civ6-mcp bridge subprocess and its HTTP surface."""

    def __init__(self, journal, cfg=None):
        self.journal = journal
        self.cfg = cfg or cfgmod.load()
        set_bridge_url(self.cfg.bridge.url)
        self.proc: subprocess.Popen | None = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def healthy(self) -> bool:
        """True when the bridge answers HTTP (link up, game connected)."""
        if not self.alive():
            return False
        return "error" not in _http("GET", "/api/overview", timeout=8.0)

    async def start(self, turn=None) -> None:
        cfg = self.cfg
        report = await asyncio.to_thread(_takeover_once, cfg)
        self.journal.add("action", {
            "tool": "bridge_takeover", "args": {}, "result": report,
        }, turn=turn)
        python = cfg.bridge.python or sys.executable or "python"
        env = os.environ.copy()
        src = cfgmod.bridge_src_path(cfg)
        if src:
            env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
        # stdin must stay OPEN (PIPE, never written) or the stdio MCP server
        # sees EOF and exits; stdout/stderr discarded so pipes never fill.
        self.proc = subprocess.Popen(
            [python, "-m", cfg.bridge.module],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, cwd=str(cfgmod.ROOT),
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        deadline = asyncio.get_event_loop().time() + cfg.bridge.ready_timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"bridge process exited rc={self.proc.returncode} "
                    "(python/env problem — is civ6-mcp installed or on PYTHONPATH?)")
            if self.healthy():
                self.journal.add("action", {
                    "tool": "bridge_up", "args": {"pid": self.proc.pid},
                    "result": f"bridge ready at {cfg.bridge.url}",
                }, turn=turn)
                return
            await asyncio.sleep(2)
        raise RuntimeError(f"bridge not ready after {cfg.bridge.ready_timeout_s}s")

    async def stop(self, turn=None) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                await asyncio.to_thread(self.proc.wait, 8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.journal.add("action", {
                "tool": "bridge_down", "args": {},
                "result": "bridge stopped — FireTuner slot released",
            }, turn=turn)
        self.proc = None

    # ── typed convenience wrappers ────────────────────────────────
    def overview(self) -> dict:
        return _http("GET", "/api/overview")

    def units(self) -> list:
        return _http("GET", "/api/units")

    def cities(self) -> dict:
        return _http("GET", "/api/cities")  # [cities, distances]

    def tech(self) -> dict:
        return _http("GET", "/api/tech")

    def threats(self) -> list:
        return _http("GET", "/api/threats")

    def turnstate(self) -> dict:
        return _http("GET", "/api/turnstate")

    def map_area(self, x: int, y: int, radius: int = 2) -> list:
        return _http("GET", f"/api/map?x={x}&y={y}&radius={radius}")

    def settle_candidates(self, unit_index: int) -> list:
        return _http("GET", f"/api/settle_candidates?unit_index={unit_index}")

    def district_advisor(self, city_id: int, district_type: str) -> list:
        return _http("GET",
                     f"/api/district_advisor?city_id={city_id}"
                     f"&district_type={district_type}")

    def production_options(self, city_id: int) -> list:
        r = _http("POST", "/api/action", {"tool": "list_city_production",
                                          "args": {"city_id": city_id}})
        return r.get("data") or []

    def act(self, tool: str, args: dict, timeout: float = 60.0) -> str:
        r = _http("POST", "/api/action", {"tool": tool, "args": args},
                  timeout=timeout)
        if "error" in r:
            return f"Error: {r['error']} {r.get('detail', '')}"
        return str(r.get("result", r.get("data", "")))


# ────────────────────────────── autopilot ───────────────────────────────────

class AutoPilot:
    """Runs gate→LLM→HTTP-actions→monitored-end_turn cycles through the bridge."""

    def __init__(self, journal, gate_fn, jev_fn, cfg=None):
        self.journal = journal
        self.gate_fn = gate_fn
        self.jev_fn = jev_fn  # sync; run in a thread
        self.cfg = cfg or cfgmod.load()
        set_bridge_url(self.cfg.bridge.url)
        self._stop_flag = False
        self._decide_lock = asyncio.Lock()
        self._sentinel_hot = 0.0
        self._in_turn_advance = False
        self._advance_started: float | None = None
        self._last_advance = time.monotonic()
        self._settler_plan: dict | None = None
        self._settler_step_turn: int | None = None
        self._tried_moves: set = set()
        self.on_pause: Callable[[], None] | None = None
        self.task: asyncio.Task | None = None
        self.sentinel_task: asyncio.Task | None = None
        self.status: dict = {
            "running": False, "step": "idle", "turn": None, "ticks": 0,
            "cycles": 0, "last_error": None, "started_at": None,
            "connected": False, "bridge_pid": None,
            "blocker": None, "waiting_s": None, "settler_target": None,
        }

    def status_public(self) -> dict:
        return dict(self.status)

    def _set(self, **kw) -> None:
        self.status.update(**kw)

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self._stop_flag = False
        self._last_advance = time.monotonic()
        self.status.update(running=True, step="starting", last_error=None,
                           started_at=_now())
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_flag = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None
        self.status.update(running=False, step="stopped", connected=False,
                           bridge_pid=None, blocker=None, waiting_s=None)

    def _pause(self, reason: str | None = None) -> None:
        """Pause the pilot with a human-readable reason (journal + UI)."""
        limit = self.cfg.autopilot.fail_limit or DEFAULT_FAIL_LIMIT
        self.status["step"] = "auto_paused"
        if reason:
            self.status["last_error"] = reason
        self.journal.add("action", {
            "tool": "control_mode", "args": {"mode": "manual"},
            "result": (f"auto-paused: {reason}" if reason
                       else f"auto-paused after {limit} consecutive failures"),
        }, turn=self.status.get("turn"))
        if self.on_pause:
            try:
                self.on_pause()
            except Exception:  # noqa: BLE001
                pass

    async def _watch_loop(self, bridge: "Bridge") -> None:
        """Always-on monitor: cheap GameCore-only probes every ~1 s.

        - keeps the UI honest about waiting time
        - stall watchdog: pauses the pilot when a turn makes NO progress for
          stall_limit_s (the 11-hour spin of campaign #1 is impossible now)
        - safety-net decide: if research/civic is idle while nothing else is
          running (e.g. a choice surfaced late), run a decide cycle — full
          InGame reads are only ever issued OUTSIDE AI-turn processing.
        """
        ap = self.cfg.autopilot
        while not self._stop_flag:
            await asyncio.sleep(ap.sentinel_interval_s)
            try:
                qs = await asyncio.to_thread(_quick_state)
                if qs:
                    if self._in_turn_advance and self._advance_started:
                        self._set(waiting_s=int(time.monotonic() - self._advance_started))
                    if (not self._in_turn_advance and self.status.get("running")
                            and time.monotonic() - self._last_advance > ap.stall_limit_s):
                        self._pause(
                            f"stall watchdog: no turn progress for {int(ap.stall_limit_s)}s")
                        continue
                    if (not self._in_turn_advance and not self._decide_lock.locked()
                            and (not qs.get("research_set", True)
                                 or not qs.get("civic_set", True))
                            and time.monotonic() - self._sentinel_hot >= ap.sentinel_cooldown_s):
                        self._sentinel_hot = time.monotonic()
                        self._set(step="sentinel_decision")
                        async with self._decide_lock:
                            await self._decide_cycle(
                                bridge, self.status.get("last_snapshot"))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the watcher never dies
                pass

    async def _loop(self) -> None:
        bridge = Bridge(self.journal, self.cfg)
        prev_snapshot: dict | None = None
        fails = 0
        limit = self.cfg.autopilot.fail_limit or DEFAULT_FAIL_LIMIT
        try:
            try:
                self._set(step="starting bridge")
                await bridge.start(self.status.get("turn"))
                self._set(connected=True, bridge_pid=bridge.proc.pid)
                self._last_advance = time.monotonic()
            except Exception as e:  # noqa: BLE001
                self.status["last_error"] = str(e)
                self._pause(str(e))
                return
            self.sentinel_task = asyncio.create_task(self._watch_loop(bridge))
            while not self._stop_flag:
                try:
                    if not await asyncio.to_thread(bridge.healthy):
                        raise ConnectionError("bridge lost — respawning")
                    async with self._decide_lock:
                        await self._decide_cycle(bridge, prev_snapshot)
                    prev_snapshot = self.status.get("last_snapshot")
                    fails = 0
                    await self._advance_turn(bridge)
                    if self.status["step"] == "auto_paused":
                        break  # blocker/stall pause decided inside advance
                except asyncio.CancelledError:
                    raise
                except ConnectionError as e:
                    fails += 1
                    self.status["last_error"] = str(e)
                    self.journal.add("action", {
                        "tool": "autopilot_error", "args": {"fail": fails},
                        "result": str(e)[:400],
                    }, turn=self.status.get("turn"))
                    if fails >= limit:
                        self._pause()
                        break
                    self._set(step="respawning bridge", connected=False,
                              bridge_pid=None)
                    await bridge.stop(self.status.get("turn"))
                    await bridge.start(self.status.get("turn"))
                    self._set(connected=True, bridge_pid=bridge.proc.pid)
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    self.status["last_error"] = str(e)
                    self.journal.add("action", {
                        "tool": "autopilot_error", "args": {"fail": fails},
                        "result": str(e)[:400],
                    }, turn=self.status.get("turn"))
                    if fails >= limit:
                        self._pause()
                        break
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        finally:
            if self.sentinel_task:
                self.sentinel_task.cancel()
                try:
                    await self.sentinel_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                self.sentinel_task = None
            self.status["running"] = False
            self.status["connected"] = False
            self.status["bridge_pid"] = None
            self.status["waiting_s"] = None
            if self.status["step"] != "auto_paused":
                self.status["step"] = "stopped"
            await bridge.stop(self.status.get("turn"))

    # ── decide / execute ──────────────────────────────────────────

    async def _decide_cycle(self, bridge: Bridge,
                            prev_snapshot: dict | None) -> None:
        # 1. collect via HTTP reads (only ever runs OUTSIDE AI processing)
        self._set(step="collecting")
        ov, units, cities_raw, tech, threats = await asyncio.gather(
            asyncio.to_thread(bridge.overview),
            asyncio.to_thread(bridge.units),
            asyncio.to_thread(bridge.cities),
            asyncio.to_thread(bridge.tech),
            asyncio.to_thread(bridge.threats),
        )
        for r in (ov, units, cities_raw, tech, threats):
            if isinstance(r, dict) and "error" in r:
                raise ConnectionError(f"bridge read failed: {r}")
        cities = cities_raw[0] if isinstance(cities_raw, list) and cities_raw else []
        if isinstance(cities, dict):
            cities = []

        # production options for every idle city (cap 3 to bound call count)
        prod_by_city: list = []
        for c in cities:
            if not _is_building_idle(c.get("currently_building")):
                continue
            if len(prod_by_city) >= 3:
                break
            opts = await asyncio.to_thread(bridge.production_options, c["city_id"])
            if isinstance(opts, list):
                prod_by_city.append({"city_id": c.get("city_id"),
                                     "name": c.get("name"), "options": opts})

        # settle candidates when a settler stands without an active plan
        settle_candidates: list = []
        if not self._settler_plan:
            settler = next((u for u in units
                            if "SETTLER" in str(u.get("unit_type", "")).upper()),
                           None)
            if settler:
                try:
                    raw = await asyncio.to_thread(
                        bridge.settle_candidates, settler["unit_index"])
                    if isinstance(raw, list):
                        settle_candidates = [
                            {"x": c.get("x"), "y": c.get("y"),
                             "score": c.get("score"), "desc": _settle_desc(c)}
                            for c in raw[:6] if isinstance(c, dict)
                        ]
                except Exception:  # noqa: BLE001
                    log.debug("settle candidates fetch failed", exc_info=True)

        # compact map around the first city (terrain-aware judgments)
        map_tiles: list = []
        cap = cities[0] if cities else None
        if cap and cap.get("x") is not None:
            try:
                tiles = await asyncio.to_thread(
                    bridge.map_area, cap["x"], cap["y"], 2)
                if isinstance(tiles, list):
                    map_tiles = _compact_tiles(tiles)
            except Exception:  # noqa: BLE001
                log.debug("map fetch failed", exc_info=True)

        snapshot = build_snapshot(ov, units, cities, tech, threats,
                                  prod_by_city, settle_candidates, map_tiles)
        self.status["last_snapshot"] = snapshot
        self._set(turn=snapshot["turn"])
        self.journal.add("state", {"snapshot": snapshot}, turn=snapshot["turn"])

        # advance an existing settler plan once per turn
        if self._settler_plan and self._settler_step_turn != snapshot["turn"]:
            self._settler_step_turn = snapshot["turn"]
            await self._advance_settler(bridge, snapshot, snapshot["turn"])

        # 2. gate
        self._set(step="gating")
        gate = self.gate_fn(snapshot, prev_snapshot)
        self.journal.add("gate", {
            "result": {k: gate[k] for k in
                       ("should_ask", "triggers", "question_ids", "skip_reason")},
        }, turn=snapshot["turn"])

        # 3. judge only on decision points
        if gate["should_ask"]:
            self._set(step="asking_llm")
            judge_state = build_judge_state(snapshot)
            model = self.cfg.llm.model or None
            try:
                resp, latency_ms = await asyncio.to_thread(
                    self.jev_fn, judge_state, gate["questions"], model)
                err = None
            except Exception as e:  # noqa: BLE001 — journal it, then re-raise
                resp, latency_ms, err = None, None, str(e)
            self.journal.add("jev", {
                "request": {"state": judge_state, "questions": gate["questions"],
                            "model": model or self.cfg.engine_summary()},
                "response": resp, "error": err, "latency_ms": latency_ms,
                "meta": {"title": f"autopilot T{snapshot['turn']} 定向判断"},
            }, turn=snapshot["turn"])
            if err:
                raise RuntimeError(f"LLM judgment failed: {err}")
            # 4. execute via HTTP actions
            self._set(step="executing")
            await self._execute(bridge, (resp or {}).get("answers") or {},
                                gate["questions"], snapshot, snapshot["turn"])

    async def _act(self, bridge: Bridge, tool: str, args: dict, turn: int,
                   timeout: float = 60.0) -> str:
        result = await asyncio.to_thread(bridge.act, tool, args, timeout)
        self.journal.add("action", {"tool": tool, "args": args,
                                    "result": str(result)[:400]}, turn=turn)
        return str(result)

    async def _execute(self, bridge: Bridge, answers: dict, questions: dict,
                       snapshot: dict, turn: int) -> None:
        for qid, ans in answers.items():
            if not isinstance(ans, dict):
                continue
            if qid == "research_pick" and ans.get("choice"):
                await self._act(bridge, "set_research",
                                {"tech_name": ans["choice"]}, turn)
            elif qid == "civic_pick" and ans.get("choice"):
                await self._act(bridge, "set_civic",
                                {"civic_name": ans["choice"]}, turn)
            elif qid.startswith("production_pick") and ans.get("choice"):
                city_id = (questions.get(qid) or {}).get("city_id")
                choice = str(ans["choice"])
                cat = next((p for p in ("UNIT", "BUILDING", "DISTRICT", "PROJECT")
                            if choice.startswith(p + "_")), None)
                if cat and city_id is not None:
                    args = {"city_id": city_id, "item_type": cat,
                            "item_name": choice, "target_x": None, "target_y": None}
                    if cat == "DISTRICT":  # districts need a placement tile
                        try:
                            placements = await asyncio.to_thread(
                                bridge.district_advisor, city_id, choice)
                            if isinstance(placements, list) and placements:
                                p = placements[0]
                                args["target_x"] = p.get("x")
                                args["target_y"] = p.get("y")
                        except Exception:  # noqa: BLE001
                            pass
                    await self._act(bridge, "set_city_production", args, turn)
                else:
                    self.journal.add("action", {
                        "tool": "verdict:production_pick",
                        "args": {"choice": choice, "city_id": city_id},
                        "result": "unrecognized item — not executed"}, turn=turn)
            elif qid == "threat_response" and ans.get("noul") is not None:
                if ans["noul"] >= 0.5:
                    await self._respond_threat(bridge, snapshot, turn)
                else:
                    self.journal.add("action", {
                        "tool": "threat_response", "args": {"noul": ans["noul"]},
                        "result": "verdict_only — hold current orders"}, turn=turn)
            elif qid == "settle_pick" and ans.get("choice"):
                await self._start_settler(bridge, snapshot, str(ans["choice"]), turn)
                await self._advance_settler(bridge, snapshot, turn)
            else:
                self.journal.add("action", {
                    "tool": f"verdict:{qid}",
                    "args": {"answer": ans.get("choice", ans.get("noul"))},
                    "result": "verdict_only — no executor for this question yet"},
                    turn=turn)

    # ── settler plan ──────────────────────────────────────────────

    async def _start_settler(self, bridge: Bridge, snapshot: dict,
                             choice: str, turn: int) -> None:
        m = re.match(r"^\s*(-?\d+)\s*,\s*(-?\d+)\s*$", choice)
        if not m:
            self.journal.add("action", {
                "tool": "plan_settle", "args": {"choice": choice},
                "result": "unparseable site"}, turn=turn)
            return
        tx, ty = int(m.group(1)), int(m.group(2))
        settler = next((u for u in (snapshot.get("units") or [])
                        if "SETTLER" in str(u.get("type", "")).upper()), None)
        if not settler:
            self.journal.add("action", {
                "tool": "plan_settle", "args": {"target": [tx, ty]},
                "result": "no settler found"}, turn=turn)
            return
        prev = self._settler_plan
        self._settler_plan = {"unit_index": settler["unit_index"],
                              "target": [tx, ty], "fails": 0}
        self.status["settler_target"] = [tx, ty]
        self.journal.add("action", {
            "tool": "plan_settle",
            "args": {"unit_index": settler["unit_index"], "target": [tx, ty]},
            "result": "re-targeted" if prev else "target set"}, turn=turn)

    async def _advance_settler(self, bridge: Bridge, snapshot: dict,
                               turn: int) -> None:
        plan = self._settler_plan
        if not plan:
            return
        u = next((u for u in (snapshot.get("units") or [])
                  if u.get("unit_index") == plan["unit_index"]), None)
        if u is None:
            self._settler_plan = None
            self.status["settler_target"] = None
            self.journal.add("action", {
                "tool": "plan_settle", "args": {"unit_index": plan["unit_index"]},
                "result": "settler gone — plan cleared"}, turn=turn)
            return
        at = u.get("at") or [None, None]
        tx, ty = plan["target"]
        if at[0] == tx and at[1] == ty:
            r = await self._act(bridge, "found_city",
                                {"unit_index": plan["unit_index"]}, turn)
            if "FOUNDED" in r:
                self._settler_plan = None
                self.status["settler_target"] = None
            else:
                plan["fails"] = plan.get("fails", 0) + 1
                if plan["fails"] >= 2:
                    self._settler_plan = None
                    self.status["settler_target"] = None
                    self.journal.add("action", {
                        "tool": "plan_settle", "args": {"target": [tx, ty]},
                        "result": "found_city kept failing — plan dropped"},
                        turn=turn)
            return
        r = await self._act(bridge, "move_unit", {
            "unit_index": plan["unit_index"], "target_x": tx, "target_y": ty}, turn)
        if "sleeping" in r.lower():
            await self._act(bridge, "alert_unit",
                            {"unit_index": plan["unit_index"]}, turn)
            r = await self._act(bridge, "move_unit", {
                "unit_index": plan["unit_index"], "target_x": tx, "target_y": ty},
                turn)
        if "BLOCKED" in r or r.startswith("Error"):
            plan["fails"] = plan.get("fails", 0) + 1
            if plan["fails"] >= 2:
                self._settler_plan = None
                self.status["settler_target"] = None
                self.journal.add("action", {
                    "tool": "plan_settle", "args": {"target": [tx, ty]},
                    "result": "settler keeps getting blocked — plan dropped "
                              "(next gate check will re-ask)"}, turn=turn)

    # ── threat response ───────────────────────────────────────────

    async def _respond_threat(self, bridge: Bridge, snapshot: dict,
                              turn: int) -> None:
        units = [u for u in (snapshot.get("units") or [])
                 if (u.get("cs") or 0) > 0 and (u.get("moves") or 0) > 0]
        # 1) engine-verified attack available?
        attacker = next((u for u in units if u.get("targets")), None)
        if attacker:
            m = re.match(r"([A-Z_]+)@(\d+),(\d+)", str(attacker["targets"][0]))
            if m:
                tx, ty = int(m.group(2)), int(m.group(3))
                r = await self._act(bridge, "attack_unit", {
                    "unit_index": attacker["unit_index"],
                    "target_x": tx, "target_y": ty}, turn)
                if not r.startswith("Error"):
                    return
        # 2) move up to two units one step toward the nearest threat
        threats = [t for t in (snapshot.get("threats") or []) if t.get("at")]
        if not threats or not units:
            return

        def near_dist(u):
            return min(_hex_distance(u["at"][0], u["at"][1],
                                     t["at"][0], t["at"][1]) for t in threats)

        movers = sorted(units, key=lambda u: (near_dist(u), -(u.get("cs") or 0)))[:2]
        t0 = min(threats, key=lambda t: t.get("distance") or 99)
        for u in movers:
            key = (turn, u["unit_index"], t0["at"][0], t0["at"][1])
            if key in self._tried_moves:
                continue  # already tried this exact move this turn
            self._tried_moves.add(key)
            r = await self._act(bridge, "move_unit", {
                "unit_index": u["unit_index"],
                "target_x": t0["at"][0], "target_y": t0["at"][1]}, turn)
            if "sleeping" in r.lower():
                await self._act(bridge, "alert_unit",
                                {"unit_index": u["unit_index"]}, turn)
                r = await self._act(bridge, "move_unit", {
                    "unit_index": u["unit_index"],
                    "target_x": t0["at"][0], "target_y": t0["at"][1]}, turn)
            if "BLOCKED" in r or r.startswith("Error"):
                await self._act(bridge, "fortify_unit",
                                {"unit_index": u["unit_index"]}, turn)

    # ── monitored end_turn ────────────────────────────────────────

    async def _end_turn_once(self, bridge: Bridge, before) -> str:
        """Fire one end_turn call and monitor the turn state while it runs.
        Returns the bridge narration when it arrives (or an early-exit note)."""
        timeout = max(30.0, float(self.cfg.autopilot.end_turn_http_timeout_s))
        et = asyncio.create_task(asyncio.to_thread(bridge.act, "end_turn", {}, timeout))

        def _consume(t):
            try:
                t.exception()
            except Exception:  # noqa: BLE001
                pass

        et.add_done_callback(_consume)
        while not self._stop_flag:
            if et.done():
                try:
                    return str(et.result())
                except Exception as e:  # noqa: BLE001
                    return f"Error: {e}"
            await asyncio.sleep(1.0)
            qs = await asyncio.to_thread(_quick_state)
            if qs and before is not None and int(qs.get("turn", -1)) > int(before):
                # turn advanced — give the narration a moment to arrive so
                # the journal keeps its "== Events ==" detail
                grace = time.monotonic() + 20.0
                while time.monotonic() < grace and not et.done():
                    await asyncio.sleep(0.5)
                if et.done():
                    try:
                        return str(et.result())
                    except Exception as e:  # noqa: BLE001
                        return f"Error: {e}"
                return f"Turn advanced to {qs['turn']} (narration not harvested)"
        return "Error: stopped"

    async def _handle_blocker(self, bridge: Bridge, kind: str, raw: str,
                              turn) -> str:
        """Best-effort resolution of a known end_turn blocker.
        Returns 'handled' or 'unhandleable'."""
        self._set(blocker=kind)
        if kind == "world_congress":
            wc = await self._act(bridge, "get_world_congress", {}, turn)
            self.journal.add("action", {
                "tool": "wc_note",
                "args": {},
                "result": ("world congress requires votes — registering the "
                           "bridge's default strategy (option A, spread favor; "
                           "free votes still counted)"),
            }, turn=turn)
            r = await self._act(bridge, "queue_wc_votes", {"votes": []}, turn)
            return "handled" if not r.startswith("Error") else "unhandleable"
        if kind == "trade_deal":
            deals = await self._act(bridge, "get_pending_deals", {}, turn)
            if not self.cfg.autopilot.auto_decline_deals:
                return "unhandleable"
            ids = re.findall(r"other_player_id[^0-9-]*(-?\d+)", deals)
            if not ids:
                return "unhandleable"
            for pid in dict.fromkeys(ids):
                await self._act(bridge, "respond_to_deal",
                                {"other_player_id": int(pid), "accept": False}, turn)
            return "handled"
        if kind == "diplomacy":
            sessions = await self._act(bridge, "get_diplomacy_sessions", {}, turn)
            ids = re.findall(r"other_player_id[^0-9-]*(-?\d+)", sessions)
            if not ids:
                return "unhandleable"
            for pid in dict.fromkeys(ids):
                await self._act(bridge, "diplomacy_respond",
                                {"other_player_id": int(pid), "response": "EXIT"}, turn)
            return "handled"
        if kind == "blockers":
            await self._act(bridge, "dismiss_popup", {}, turn)
            if "UNITS" in raw.upper():
                await self._act(bridge, "skip_remaining_units", {}, turn)
            return "handled"
        if kind == "hang":
            await self._act(bridge, "dismiss_popup", {}, turn)
            return "handled"
        return "unhandleable"

    async def _advance_turn(self, bridge: Bridge) -> None:
        """End the current turn and wait — with continuous monitoring — until
        the game hands control back. Bounded in time; blockers get handled;
        anything stuck pauses the pilot loudly instead of spinning forever."""
        ap = self.cfg.autopilot
        self._in_turn_advance = True
        self._advance_started = time.monotonic()
        self._tried_moves = set()
        self._set(step="ending_turn", blocker=None, waiting_s=0)
        deadline = time.monotonic() + ap.turn_wait_timeout_s
        handled: dict[str, int] = {}
        total_handled = 0
        try:
            while not self._stop_flag and time.monotonic() < deadline:
                before = (self.status.get("last_snapshot") or {}).get("turn")
                result = await self._end_turn_once(bridge, before)
                if self._stop_flag:
                    return
                text = str(result or "")
                kind = classify_end_turn(text)
                if parse_turn_advance(text, before) is not None:
                    kind = "ok"
                self.journal.add("action", {
                    "tool": "end_turn", "args": {"mode": "auto"},
                    "result": text[:500]}, turn=before)
                if kind == "ok":
                    self._last_advance = time.monotonic()
                    self.status["ticks"] += 1
                    return
                if kind == "game_over":
                    self._pause("game over")
                    return
                if kind in HANDLABLE_BLOCKERS:
                    total_handled += 1
                    handled[kind] = handled.get(kind, 0) + 1
                    if handled[kind] > BLOCKER_PER_KIND_CAP or total_handled > BLOCKER_TOTAL_CAP:
                        self._pause(f"end_turn blocked repeatedly ({kind}) — manual attention needed")
                        return
                    self._set(step=f"handling_{kind}")
                    status_ = await self._handle_blocker(bridge, kind, text, before)
                    if status_ != "handled":
                        self._pause(f"blocker not resolvable: {kind}")
                        return
                    await asyncio.sleep(1.0)
                    continue
                if kind in ("timeout", "error"):
                    if kind == "error" and not await asyncio.to_thread(bridge.healthy):
                        raise ConnectionError(f"end_turn transport failed: {text[:150]}")
                    await asyncio.sleep(1.5)
                    continue
                await asyncio.sleep(1.5)  # unknown shape — retry within budget
            if not self._stop_flag:
                self._pause(
                    f"turn did not advance within {int(ap.turn_wait_timeout_s)}s")
        finally:
            self._in_turn_advance = False
            self._advance_started = None
            self._set(waiting_s=None)
