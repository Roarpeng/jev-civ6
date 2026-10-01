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

HANDLABLE_BLOCKERS = ("world_congress", "trade_deal", "diplomacy", "dedication",
                      "great_person", "policy_fill", "governor",
                      "blockers", "hang")


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
    try:
        # Best-effort probe: PowerShell's first launch can exceed 15s under
        # game load — a slow/failed probe must not abort the bridge spawn
        # (the worst case is an old controller holding the tuner slot, which
        # surfaces as a handshake failure we can retry).
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True,
                             timeout=45).stdout
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"takeover probe skipped ({type(e).__name__})"
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
    if ("diplomacy" in low and ("pending" in low or "cannot end turn" in low)) \
            or "diplomatic proposal" in low:
        return "diplomacy"
    if r.startswith("FAST_NO_ADVANCE"):
        # fast end_turn hit its poll cap — same handling as a timeout:
        # brief sleep, retry (the in-flight request stays valid), and the
        # consecutive-timeout unblock fuse applies.
        return "timeout"
    if ("commemoration" in low) or ("dedication" in low):
        return "dedication"
    if "great person" in low:
        return "great_person"
    if "policy slots empty" in low or "policy slot empty" in low:
        return "policy_fill"
    if "governor titles available" in low:
        return "governor"
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
             "amenities": c.get("amenities"),
             "housing": c.get("housing"),
             "loyalty": c.get("loyalty"),
             "loyalty_per_turn": c.get("loyalty_per_turn"),
             "production": (str(c.get("currently_building"))
                            if not _is_building_idle(c.get("currently_building"))
                            else ""),
             "defense": c.get("defense_strength"),
             "garrison": c.get("garrison_unit"),
             "districts": c.get("districts") or [],
             "buildings": c.get("buildings") or [],
             "unimproved_resources": c.get("unimproved_resources") or []}
            for c in cities
        ],
        "units": [
            {"unit_index": u.get("unit_index"), "type": u.get("unit_type"),
             "at": [u.get("x"), u.get("y")], "cs": u.get("combat_strength"),
             "hp": u.get("health"), "max_hp": u.get("max_health"),
             "moves": u.get("moves_remaining"), "targets": u.get("targets") or [],
             "valid_improvements": u.get("valid_improvements") or []}
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
    strategy = s.get("strategy") or {}
    if strategy.get("path"):
        situation += (f" | STRATEGY: {strategy['path']} victory "
                      f"(since T{strategy.get('since_turn')})")
    return {
        "situation": situation,
        "empire": {"yields": yields, "cities": cities, "units": units},
        "threats": s.get("threats") or [],
        "map_near_capital": s.get("map_near_capital") or None,
        "settle_candidates": s.get("settle_candidates") or None,
        "available": s.get("available"),
        "notes": s.get("notes") or [],
    }


def _policy_options(slot_type: str, avail: list, cap: int = 12) -> dict:
    """Compatible policy options for one slot (wildcard accepts anything)."""
    wild = str(slot_type).upper() == "SLOT_WILDCARD"
    out: dict = {}
    for p in avail:
        if not isinstance(p, dict):
            continue
        if wild or p.get("slot_type") == slot_type:
            out[p.get("policy_type")] = (
                f"{p.get('name') or '?'} — {str(p.get('description') or '')[:70]}")
            if len(out) >= cap:
                break
    return out


def _unimproved_tiles(snapshot: dict) -> list:
    """[(x, y), ...] of unimproved resource tiles from city data. The bridge
    splits each 'RES@x,y' entry at the comma, so entries arrive in pairs.
    City centres (auto-worked, unbuildable) and tiles occupied by any unit
    (own or hostile — stacking blocks the move) are excluded."""
    blocked: set = set()
    for c in (snapshot.get("cities") or []):
        at = c.get("at")
        if at:
            blocked.add((at[0], at[1]))
    for u in (snapshot.get("units") or []):
        at = u.get("at")
        if at:
            blocked.add((at[0], at[1]))
    for t in (snapshot.get("threats") or []):
        at = t.get("at")
        if at:
            blocked.add((at[0], at[1]))
    tiles: list = []
    for c in (snapshot.get("cities") or []):
        raw = [str(v) for v in (c.get("unimproved_resources") or [])]
        for i in range(0, len(raw) - 1, 2):
            m = re.match(r"^[A-Z_]+@(-?\d+)$", raw[i])
            if m and raw[i + 1].lstrip("-").isdigit():
                xy = (int(m.group(1)), int(raw[i + 1]))
                if xy not in blocked:
                    tiles.append(xy)
    return tiles


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

    def diplomacy(self) -> dict:
        return _http("GET", "/api/diplomacy")

    def turnstate(self) -> dict:
        return _http("GET", "/api/turnstate")

    def warroom_collect(self, pol: bool = False, cs: bool = False,
                        gov: bool = False) -> dict:
        """Whole per-turn collect in ONE batched Lua roundtrip; low-frequency
        sections (policies/city-states/governors) only when flagged."""
        q = []
        if pol:
            q.append("pol=1")
        if cs:
            q.append("cs=1")
        if gov:
            q.append("gov=1")
        suffix = ("?" + "&".join(q)) if q else ""
        return _http("GET", "/api/warroom_collect" + suffix)

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

    def act_data(self, tool: str, args: dict, timeout: float = 60.0) -> list:
        """Action endpoint variant that returns structured (list) data."""
        r = _http("POST", "/api/action", {"tool": tool, "args": args},
                  timeout=timeout)
        if "error" in r:
            return [{"unit_index": None,
                     "result": f"Error: {r['error']} {r.get('detail', '')}"}]
        data = r.get("data")
        return data if isinstance(data, (list, dict)) else []


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
        self._settler_park_turn: int | None = None
        self._borders_step_turn: int | None = None
        self._borders_blocked: dict[str, int] = {}
        self._borders_tried: dict[str, int] = {}
        self._strategy: dict | None = None   # {"path": "science", "since_turn": N}
        self._religion_founding_started = False
        self._last_policy_review: int | None = None
        self._builder_step_turn: int | None = None
        self._gov_step_turn: int | None = None
        self._last_game_event_names: set = set()
        self._faith_step_turn: int | None = None
        self._known_foreign_cities: dict = {}   # "x,y" -> {"owner": pid}
        self._faith_cooldown: int | None = None
        self._tried_moves: set = set()
        self._load_state()
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

    # ── cross-restart persistence (strategy & review bookkeeping) ──

    _STATE_FILE = "autopilot_state.json"

    def _load_state(self) -> None:
        try:
            with open(self._STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self._strategy = data.get("strategy")
            self._last_policy_review = data.get("last_policy_review")
            self._religion_founding_started = bool(
                data.get("religion_founding_started"))
        except Exception:  # noqa: BLE001 — fresh start is fine
            pass

    def _save_state(self) -> None:
        try:
            with open(self._STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"strategy": self._strategy,
                           "last_policy_review": self._last_policy_review,
                           "religion_founding_started":
                               self._religion_founding_started},
                          f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            log.debug("autopilot state save failed", exc_info=True)

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

    def _pause_screenshot(self, reason) -> None:
        """Capture the game screen when the pilot pauses — visual evidence
        for post-mortem (what was the game showing at the failure?)."""
        try:
            import pathlib
            from scripts.game_screenshot import capture
            d = pathlib.Path("artifacts/screenshots")
            d.mkdir(parents=True, exist_ok=True)
            turn = self.status.get("turn")
            path = d / f"T{turn}_{int(time.time())}.png"
            if capture(str(path)):
                self.journal.add("action", {
                    "tool": "error_screenshot",
                    "args": {"reason": str(reason)[:80]},
                    "result": str(path)}, turn=turn)
        except Exception:  # noqa: BLE001 — never break pausing on this
            log.debug("pause screenshot failed", exc_info=True)

    def _pause(self, reason: str | None = None) -> None:
        """Pause the pilot with a human-readable reason (journal + UI)."""
        limit = self.cfg.autopilot.fail_limit or DEFAULT_FAIL_LIMIT
        self.status["step"] = "auto_paused"
        if reason:
            self._pause_screenshot(reason)
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
        # 1. collect — the whole state pull is TWO batched Lua roundtrips
        #    (bridge /api/warroom_collect); only ever runs OUTSIDE AI processing
        self._set(step="collecting")
        turn_hint = (prev_snapshot or {}).get("turn") or 0
        ev_names = self._last_game_event_names
        want_pol = ((turn_hint - (self._last_policy_review or -999)) >= 13
                    or any("CivicCompleted" in n for n in ev_names))
        want_cs = (turn_hint % 5 == 0
                   or any("InfluenceChanged" in n for n in ev_names))
        want_gov = (turn_hint % 10 == 0
                    or any("Governor" in n for n in ev_names))
        col = await asyncio.to_thread(
            bridge.warroom_collect, want_pol, want_cs, want_gov)
        if not isinstance(col, dict) or "error" in col:
            raise ConnectionError(f"bridge collect failed: {col}")
        ov = col.get("overview") or {}
        units = col.get("units") or []
        cities_raw = col.get("cities") or []
        tech = col.get("tech") or {}
        threats = col.get("threats") or []
        wr = col.get("wr") or {}
        game_events = wr.get("events") or []
        self._last_game_event_names = {
            str(e.get("name")) for e in game_events}
        if wr.get("event_names"):
            # one-time discovery dump from the resident framework — log it so
            # the curated hook list can be extended from real game data
            self.journal.add("game_event", {
                "discovered_event_names": wr["event_names"],
                "count": len(wr["event_names"]),
                "footer": wr.get("footer", ""),
            }, turn=None)
        if game_events:
            self.journal.add("game_event", {
                "events": game_events[:120],
                "count": len(game_events),
            }, turn=(game_events[0] or {}).get("turn"))
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
        # victory strategy (LLM-decided, pilot-remembered) + policy cards.
        # The game pre-fills default policies, so slots are never empty —
        # instead we REVIEW them periodically (policy_review trigger).
        snapshot["strategy"] = self._strategy
        pol = col.get("policies") or {}
        if isinstance(pol, dict):
            slots, opts_by = [], {}
            for s in (pol.get("slots") or []):
                st = s.get("slot_type") or ""
                cur = s.get("current_policy")
                slots.append({"slot_index": s.get("slot_index"),
                              "slot_type": st, "current": cur})
                o = _policy_options(st, pol.get("available_policies") or [])
                if cur:
                    o[cur] = str(o.get(cur) or cur) + " (current)"
                if o:
                    opts_by[str(s.get("slot_index"))] = o
            if slots:
                snapshot["policies"] = {
                    "government": pol.get("government_name"),
                    "slots": slots, "options_by_slot": opts_by,
                    "review_age": ((snapshot.get("turn") or 0)
                                   - (self._last_policy_review or -999)),
                }
        # city-states + envoy tokens (diplomacy decision channel)
        cs = col.get("city_states") or {}
        if isinstance(cs, dict) and (cs.get("tokens_available") or 0) > 0:
            sendable = [s for s in (cs.get("city_states") or [])
                        if isinstance(s, dict) and s.get("can_send_envoy")]
            if sendable:
                snapshot["city_states"] = sendable
        # surface this turn's game events (resident framework) to the gate
        # and the judge: decisions worth making often ANNOUNCE themselves
        me_owner = None  # our player id: own cities carry city_id 65536+
        for e in game_events:
            if e.get("name") in ("CityAddedToMap", "CityInitialized"):
                parts = str(e.get("detail") or "").split(",")
                if len(parts) >= 4:
                    try:
                        pid = int(parts[0])
                        x, y = int(parts[-2]), int(parts[-1])
                        self._known_foreign_cities[f"{x},{y}"] = {"owner": pid}
                    except ValueError:
                        pass
        if game_events:
            key = [f"GAME_EVENT {e.get('name')} {e.get('detail', '')}".strip()
                   for e in game_events[:12]]
            snapshot["notes"] = list(snapshot.get("notes") or []) + key
        self.status["last_snapshot"] = snapshot
        self._set(turn=snapshot["turn"])
        self.journal.add("state", {"snapshot": snapshot}, turn=snapshot["turn"])

        # advance an existing settler plan once per turn
        if self._settler_plan and self._settler_step_turn != snapshot["turn"]:
            self._settler_step_turn = snapshot["turn"]
            await self._advance_settler(bridge, snapshot, snapshot["turn"])

        # park a plan-less settler off its city tile: one standing ON the
        # city center blocks every faith/unit purchase (STACKING_CONFLICT)
        # and with no settle candidates it would idle there forever.
        if (not self._settler_plan
                and not (snapshot.get("settle_candidates") or [])
                and self._settler_park_turn != snapshot["turn"]):
            for u in (snapshot.get("units") or []):
                if "SETTLER" not in str(u.get("type", "")).upper():
                    continue
                ux, uy = u.get("at") or (None, None)
                if ux is None:
                    continue
                if not any((c.get("at") or [None, None])[0] == ux
                           and (c.get("at") or [None, None])[1] == uy
                           for c in (snapshot.get("cities") or [])):
                    continue  # not on a city tile
                if (u.get("moves") or 0) <= 0:
                    break
                self._settler_park_turn = snapshot["turn"]
                for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0),
                               (1, -1), (1, 1), (-1, 1), (-1, -1)):
                    r = await self._act(bridge, "move_unit", {
                        "unit_index": u["unit_index"],
                        "target_x": ux + dx, "target_y": uy + dy},
                        snapshot["turn"])
                    if "now_at" in r.lower():
                        self.journal.add("action", {
                            "tool": "settler_park", "args": {
                                "unit_index": u["unit_index"],
                                "to": [ux + dx, uy + dy]},
                            "result": ("moved idle settler off the city tile "
                                       "(was blocking all purchases)")},
                            turn=snapshot["turn"])
                        break
                break

        # builders: deterministic economy layer (no LLM needed) — improve the
        # tile under them or walk toward the nearest unimproved resource
        if self._builder_step_turn != snapshot["turn"]:
            self._builder_step_turn = snapshot["turn"]
            await self._advance_builders(bridge, snapshot, snapshot["turn"])

        # Great Prophet → walk to our Holy Site and FOUND THE RELIGION
        # (the defining move of the religion victory path)
        await self._advance_prophet(bridge, snapshot, snapshot["turn"])

        # religion spread engine: buy missionaries/apostles with faith,
        # spread when in position, walk toward known foreign cities
        await self._spread_religion(bridge, snapshot, snapshot["turn"])

        # unblock cross-border spreading: propose mutual open borders with
        # civs whose territory our religious units keep bouncing off
        await self._request_open_borders(bridge, snapshot, snapshot["turn"])

        # governor maintenance: station unassigned governors into cities
        # that have NONE (one governor per city — piling on displaces!), 
        # lowest-loyalty city first; max one attempt per turn (assignments
        # are async and a stale re-read would churn)
        gov = col.get("governors") or {}
        all_appointed = [g for g in (gov.get("appointed") or [])
                         if isinstance(g, dict)]
        occupied = {g.get("assigned_city_id") for g in all_appointed
                    if g.get("assigned_city_id") not in (-1, None)}
        unassigned = [g for g in all_appointed
                      if g.get("assigned_city_id") in (-1, None)]
        free_cities = [c for c in (snapshot.get("cities") or [])
                       if c.get("city_id") is not None
                       and c.get("city_id") not in occupied]
        if (unassigned and free_cities
                and self._gov_step_turn != snapshot["turn"]):
            self._gov_step_turn = snapshot["turn"]
            free_cities.sort(key=lambda c: (c.get("loyalty")
                             if isinstance(c.get("loyalty"), (int, float))
                             else 999))
            for g, target in zip(unassigned, free_cities):
                await self._act(bridge, "assign_governor", {
                    "governor_type": g.get("governor_type"),
                    "city_id": target.get("city_id")}, turn=snapshot["turn"])

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
        tactics_answers: list[tuple[str, dict]] = []
        policy_assignments: dict[str, str] = {}
        for qid, ans in answers.items():
            if not isinstance(ans, dict):
                continue
            if qid.startswith("tactics:"):
                tactics_answers.append((qid, ans))
                continue
            if qid == "strategy_pick" and ans.get("choice"):
                self._strategy = {"path": str(ans["choice"]),
                                  "since_turn": turn}
                self._save_state()
                self.journal.add("action", {
                    "tool": "strategy", "args": dict(self._strategy),
                    "result": f"victory path: {ans['choice']}"}, turn=turn)
                continue
            if qid.startswith("policy_pick:") and ans.get("choice"):
                policy_assignments[qid.split(":", 1)[1]] = str(ans["choice"])
                continue
            if qid == "envoy_pick" and ans.get("choice"):
                await self._act(bridge, "send_envoy",
                                {"city_state_player_id": int(ans["choice"])},
                                turn)
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
            elif qid == "settle_pick" and ans.get("choice"):
                await self._start_settler(bridge, snapshot, str(ans["choice"]), turn)
                await self._advance_settler(bridge, snapshot, turn)
            else:
                self.journal.add("action", {
                    "tool": f"verdict:{qid}",
                    "args": {"answer": ans.get("choice", ans.get("noul"))},
                    "result": "verdict_only — no executor for this question yet"},
                    turn=turn)

        # policy review: one set_policies call for all real changes
        if policy_assignments:
            cur_by = {str(s.get("slot_index")): s.get("current")
                      for s in ((snapshot.get("policies") or {}).get("slots")
                                or [])}
            changes = {k: v for k, v in policy_assignments.items()
                       if v != cur_by.get(k)}
            if changes:
                await self._act(bridge, "set_policies",
                                {"assignments": changes}, turn)
            else:
                self.journal.add("action", {
                    "tool": "set_policies", "args": {},
                    "result": "review complete — current policies kept"},
                    turn=turn)
            self._last_policy_review = turn
            self._save_state()

        if tactics_answers:
            await self._execute_tactics_batch(
                bridge, tactics_answers, questions, snapshot, turn)

    async def _execute_tactics_batch(self, bridge: Bridge,
                                     tactics_answers: list[tuple[str, dict]],
                                     questions: dict, snapshot: dict,
                                     turn: int) -> None:
        """Turn per-unit Jev tactics into a handful of batched bridge calls:
        one move_units_batch roundtrip for all advances/retreats, one
        fortify_units roundtrip for all holds (plus the default-fortify
        units the gate never asked about), attack_unit per attack (already
        internally batched)."""
        units_by_idx = {str(u.get("unit_index")): u
                        for u in (snapshot.get("units") or [])}
        move_intents: list[dict] = []
        fortify_ids: list[int] = []
        attack_jobs: list[tuple[dict, int, int]] = []

        for qid, ans in tactics_answers:
            idx = qid.split(":", 1)[1]
            unit = units_by_idx.get(idx)
            choice = str(ans.get("choice", "")).strip().lower()
            if unit is None:
                self.journal.add("action", {
                    "tool": f"verdict:{qid}", "args": {"choice": ans.get("choice")},
                    "result": "unit not found in snapshot — skipped"}, turn=turn)
                continue
            ui = unit.get("unit_index")
            if choice.startswith("attack:"):
                m = re.match(r"attack:(-?\d+),(-?\d+)$", choice)
                if m:
                    attack_jobs.append((unit, int(m.group(1)), int(m.group(2))))
                else:
                    fortify_ids.append(ui)
            elif choice == "advance":
                threats = [t for t in (snapshot.get("threats") or [])
                           if t.get("at")]
                if threats:
                    t0 = min(threats, key=lambda t: t.get("distance") or 99)
                    move_intents.append({"unit_index": ui,
                                         "target_x": t0["at"][0],
                                         "target_y": t0["at"][1]})
                else:
                    fortify_ids.append(ui)
            elif choice == "retreat":
                city = next((c for c in (snapshot.get("cities") or [])
                             if c.get("at")), None)
                if city:
                    move_intents.append({"unit_index": ui,
                                         "target_x": city["at"][0],
                                         "target_y": city["at"][1]})
                else:
                    fortify_ids.append(ui)
            else:  # fortify and anything unrecognised: hold and defend
                fortify_ids.append(ui)

        # attacks first (kill opportunities shouldn't wait on marches)
        for unit, tx, ty in attack_jobs:
            r = await self._act(bridge, "attack_unit", {
                "unit_index": unit.get("unit_index"),
                "target_x": tx, "target_y": ty}, turn)
            if r.startswith("Error"):
                await self._act(bridge, "fortify_unit",
                                {"unit_index": unit.get("unit_index")}, turn)

        # all marches in ONE roundtrip (per-unit dedup preserved)
        filtered: list[dict] = []
        for m in move_intents:
            key = (turn, m["unit_index"], m["target_x"], m["target_y"])
            if key in self._tried_moves:
                continue
            self._tried_moves.add(key)
            filtered.append(m)
        if filtered:
            results = await asyncio.to_thread(
                bridge.act_data, "move_units_batch", {"moves": filtered})
            summary = "\n".join(
                f"u{r.get('unit_index')}: {str(r.get('result'))[:120]}"
                for r in results)
            self.journal.add("action", {
                "tool": "move_units_batch",
                "args": {"moves": filtered},
                "result": summary[:1500]}, turn=turn)
            for r in results:
                res = str(r.get("result", ""))
                if r.get("unit_index") is None:
                    continue  # whole-batch transport error — nothing to salvage
                if res.startswith("Error") or "BLOCKED" in res:
                    await self._act(bridge, "fortify_unit",
                                    {"unit_index": r.get("unit_index")}, turn)

        # default orders: un-asked military units fortify too (heal/defend)
        if any(q.startswith("tactics:") for q in questions):
            asked = {q.split(":", 1)[1] for q in questions
                     if q.startswith("tactics:")}
            for u in (snapshot.get("units") or []):
                if (("SETTLER" in str(u.get("type", "")).upper())
                        or (u.get("cs") or 0) <= 0 or (u.get("moves") or 0) <= 0):
                    continue
                if str(u.get("unit_index")) in asked:
                    continue
                if u.get("unit_index") not in fortify_ids:
                    fortify_ids.append(u.get("unit_index"))

        fortify_ids = [i for i in dict.fromkeys(fortify_ids) if i is not None]
        if fortify_ids:
            results = await asyncio.to_thread(
                bridge.act_data, "fortify_units", {"unit_indexes": fortify_ids})
            summary = "\n".join(
                f"u{r.get('unit_index')}: {str(r.get('result'))[:80]}"
                for r in results)
            self.journal.add("action", {
                "tool": "fortify_units",
                "args": {"unit_indexes": fortify_ids},
                "result": summary[:1000]}, turn=turn)

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

    # ── religion spread engine (faith → apostles/missionaries) ─────

    async def _spread_religion(self, bridge: Bridge, snapshot: dict,
                               turn: int) -> None:
        if self._faith_step_turn == turn:
            return
        self._faith_step_turn = turn
        faith = (snapshot.get("yields") or {}).get("faith") or 0
        cap = next((c for c in (snapshot.get("cities") or [])
                    if any("HOLY_SITE" in str(d) for d in
                           (c.get("districts") or []))), None)
        # 1. buy — apostles first (3 spreads + theological combat); keep a
        #    buffer so emergency purchases stay possible
        if (cap and faith >= 700
                and (self._faith_cooldown or 0) < turn):
            r = await self._act(bridge, "purchase_item", {
                "city_id": cap["city_id"], "item_type": "UNIT",
                "item_name": "UNIT_APOSTLE", "yield_type": "YIELD_FAITH"},
                turn)
            if "Error" in r and faith >= 350:
                r = await self._act(bridge, "purchase_item", {
                    "city_id": cap["city_id"], "item_type": "UNIT",
                    "item_name": "UNIT_MISSIONARY",
                    "yield_type": "YIELD_FAITH"}, turn)
            if "Error" not in r:
                self.journal.add("action", {
                    "tool": "faith_purchase", "args": {"result_of": r[:60]},
                    "result": str(r)[:120]}, turn=turn)
            elif "CANNOT_PURCHASE" in r:
                # usually 'no Shrine/Temple in the Holy Site' — cool down
                # and let the production nudge build them
                self._faith_cooldown = turn + 5
                self.journal.add("action", {
                    "tool": "faith_purchase", "args": {"blocked": True},
                    "result": "CANNOT_PURCHASE — Shrine/Temple missing; "
                              "production nudged"}, turn=turn)
        # 2. act with religious units already on the map
        targets = []
        for k in self._known_foreign_cities:
            m = re.match(r"^(-?\d+),(-?\d+)$", k)
            if m:
                targets.append((int(m.group(1)), int(m.group(2))))
        # spread only works in/adjacent to a city — skip the doomed attempt
        # (and its error roundtrip) when the unit stands elsewhere (e.g. on
        # the Holy Site district tile).
        city_pts = targets + [tuple(c.get("at") or (None, None))
                              for c in (snapshot.get("cities") or [])]
        for u in (snapshot.get("units") or []):
            t = str(u.get("type", "")).upper()
            if not ("MISSIONARY" in t or "APOSTLE" in t):
                continue
            ui = u.get("unit_index")
            at = u.get("at") or [0, 0]
            near_city = any(
                p[0] is not None and _hex_distance(
                    at[0], at[1], p[0], p[1]) <= 1
                for p in city_pts)
            r = ""
            if near_city:
                r = await self._act(bridge, "spread_religion",
                                    {"unit_index": ui}, turn)
                if "Error" not in r:
                    self.journal.add("action", {
                        "tool": "spread_religion", "args": {"unit_index": ui},
                        "result": str(r)[:120]}, turn=turn)
                    continue
            if targets:
                at2 = u.get("at") or [0, 0]
                t0 = min(targets, key=lambda xy: _hex_distance(
                    at2[0], at2[1], xy[0], xy[1]))
                key = (turn, ui, t0[0], t0[1])
                if key not in self._tried_moves:
                    self._tried_moves.add(key)
                    mv = await self._act(bridge, "move_unit", {
                        "unit_index": ui,
                        "target_x": t0[0], "target_y": t0[1]}, turn)
                    if "need Open Borders" in mv or "foreign territory" in mv:
                        m2 = re.search(
                            r"foreign territory \(([^)]+)\)", mv)
                        if m2:
                            civ = m2.group(1).strip()
                            self._borders_blocked[civ] = turn

    async def _request_open_borders(self, bridge: Bridge, snapshot: dict,
                                    turn: int) -> None:
        """Propose mutual open borders with civs whose territory blocks our
        religious units (once per civ per 20 turns; skip if at war)."""
        if self._borders_step_turn == turn or not self._borders_blocked:
            return
        self._borders_step_turn = turn
        wanted = {civ for civ, t in self._borders_blocked.items()
                  if turn - t <= 10}
        if not wanted:
            return
        try:
            dip = await asyncio.to_thread(bridge.diplomacy)
        except Exception:  # noqa: BLE001
            log.debug("diplomacy fetch failed", exc_info=True)
            return
        data = dip.get("data") if isinstance(dip, dict) else dip
        rows = (data or {}).get("civs") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("civ_name") or "").strip()
            pid = row.get("player_id")
            if not name or pid is None or name not in wanted:
                continue
            if str(row.get("war") or "0") == "1":
                continue
            if (self._borders_tried.get(name, -99) + 20) > turn:
                continue
            self._borders_tried[name] = turn
            r = await self._act(bridge, "send_diplomatic_action", {
                "other_player_id": int(pid),
                "action": "OPEN_BORDERS"}, turn)
            self.journal.add("action", {
                "tool": "open_borders", "args": {"civ": name,
                                                 "player_id": pid},
                "result": str(r)[:150]}, turn=turn)

    # ── great prophet → found religion ─────────────────────────────

    async def _advance_prophet(self, bridge: Bridge, snapshot: dict,
                               turn: int) -> None:
        prophet = next((u for u in (snapshot.get("units") or [])
                        if "GREAT_PROPHET" in str(u.get("type", "")).upper()),
                       None)
        if prophet is None:
            # prophet already consumed by a previous activation — the
            # religion/belief selection may still be pending
            if self._religion_founding_started:
                await self._found_religion(bridge, turn)
            return
        hs = None
        for c in (snapshot.get("cities") or []):
            for d in (c.get("districts") or []):
                m = re.match(r".*?HOLY_SITE@(-?\d+),(-?\d+)", str(d))
                if m:
                    hs = (int(m.group(1)), int(m.group(2)))
        if hs is None:
            return
        at = prophet.get("at") or [0, 0]
        if (at[0], at[1]) != hs:
            key = (turn, prophet.get("unit_index"), hs[0], hs[1])
            if key in self._tried_moves:
                return
            self._tried_moves.add(key)
            r = await self._act(bridge, "move_unit", {
                "unit_index": prophet.get("unit_index"),
                "target_x": hs[0], "target_y": hs[1]}, turn)
            self.journal.add("action", {
                "tool": "prophet", "args": {"moving_to": list(hs)},
                "result": str(r)[:120]}, turn=turn)
            return
        # standing on the Holy Site — activate, then complete the founding
        r = await self._act(bridge, "activate_great_person",
                            {"unit_index": prophet.get("unit_index")}, turn)
        self.journal.add("action", {
            "tool": "activate_great_person",
            "args": {"unit_index": prophet.get("unit_index")},
            "result": str(r)[:150]}, turn=turn)
        if "Error" in r:
            return
        self._religion_founding_started = True
        await asyncio.sleep(1.5)
        await self._found_religion(bridge, turn)

    async def _found_religion(self, bridge: Bridge, turn: int) -> None:
        try:
            st = await asyncio.to_thread(bridge.act_data,
                                         "get_religion_founding_status", {})
        except Exception:
            st = None
        if not isinstance(st, dict):
            return
        # pantheon first if missing
        if not st.get("pantheon_index") or st.get("pantheon_index", -1) < 0:
            beliefs = (st.get("beliefs_by_class") or {}).get(
                "PANTHEON") or []
            if beliefs:
                ans = await self._ask_judge_inline(
                    {"pantheon_pick": {
                        "type": "choice",
                        "instructions": (
                            "Pick our PANTHEON belief (founding faith "
                            "step; boosts our empire broadly)."),
                        "criteria": {
                            b.get("belief_type", str(i)):
                                f"{b.get('name', '?')} — "
                                f"{str(b.get('description') or '')[:100]}"
                            for i, b in enumerate(beliefs)
                            if isinstance(b, dict)
                        },
                    }}, turn, situation="Pantheon belief")
                pick = (ans or {}).get("pantheon_pick", {}).get("choice")
                if not pick and beliefs:
                    b0 = beliefs[0]
                    pick = b0.get("belief_type") if isinstance(b0, dict) else None
                if pick:
                    await self._act(bridge, "choose_pantheon",
                                    {"belief_type": str(pick)}, turn)
                    await asyncio.sleep(1.0)
                    st = await asyncio.to_thread(
                        bridge.act_data,
                        "get_religion_founding_status", {})
                    st = st if isinstance(st, dict) else {}
        if st.get("has_religion"):
            return
        religions = st.get("available_religions") or []
        if not religions:
            return
        by_class = st.get("beliefs_by_class") or {}
        founder: list = []
        follower: list = []
        for k, v in by_class.items():
            ku = str(k).upper()
            if "FOUNDER" in ku:
                founder = v or []
            elif "FOLLOWER" in ku:
                follower = v or []
        if not founder or not follower:
            return
        questions = {
            "religion_pick": {
                "type": "choice",
                "instructions": "Name our new RELIGION (flavour choice).",
                "criteria": {r[0]: r[1] for r in religions
                             if isinstance(r, (list, tuple)) and len(r) >= 2},
            },
            "founder_belief": {
                "type": "choice",
                "instructions": (
                    "Pick the FOUNDER belief (benefits only us, the founder "
                    "of the religion)."),
                "criteria": {b.get("belief_type"): f"{b.get('name')} — "
                            f"{str(b.get('description') or '')[:100]}"
                            for b in founder if isinstance(b, dict)},
            },
            "follower_belief": {
                "type": "choice",
                "instructions": (
                    "Pick the FOLLOWER belief (benefits every city that "
                    "follows the religion — drives conversion value)."),
                "criteria": {b.get("belief_type"): f"{b.get('name')} — "
                            f"{str(b.get('description') or '')[:100]}"
                            for b in follower if isinstance(b, dict)},
            },
        }
        ans = await self._ask_judge_inline(questions, turn,
                                           situation="FOUNDING OUR RELIGION")
        answers = ans or {}
        rel = (answers.get("religion_pick") or {}).get("choice")             or religions[0][0]
        fb = (answers.get("founder_belief") or {}).get("choice")             or founder[0].get("belief_type")
        flb = (answers.get("follower_belief") or {}).get("choice")             or follower[0].get("belief_type")
        r = await self._act(bridge, "found_religion", {
            "religion_type": str(rel),
            "follower_belief": str(flb),
            "founder_belief": str(fb)}, turn)
        if "Error" not in r:
            self._religion_founding_started = False
        self.journal.add("action", {
            "tool": "found_religion",
            "args": {"religion": str(rel), "founder": str(fb),
                     "follower": str(flb)},
            "result": str(r)[:200]}, turn=turn)

    # ── builders: deterministic economy layer ─────────────────────

    async def _advance_builders(self, bridge: Bridge, snapshot: dict,
                                turn: int) -> None:
        """Idle builders improve the tile they stand on (engine-validated
        options) or walk toward the nearest unimproved resource tile.
        No LLM involved — this is reflex-layer housekeeping."""
        builders = [u for u in (snapshot.get("units") or [])
                    if "BUILDER" in str(u.get("type", "")).upper()
                    and (u.get("moves") or 0) > 0]
        if not builders:
            return
        targets = _unimproved_tiles(snapshot)
        for b in builders:
            ui = b.get("unit_index")
            key = (turn, ui, "builder")
            if key in self._tried_moves:
                continue
            imps = b.get("valid_improvements") or []
            if imps:
                self._tried_moves.add(key)
                r = await self._act(bridge, "improve_tile",
                                    {"unit_index": ui,
                                     "improvement_name": imps[0]}, turn)
                self.journal.add("action", {
                    "tool": "builder", "args": {"unit_index": ui,
                                                "improvement": imps[0]},
                    "result": str(r)[:120]}, turn=turn)
                continue
            if targets:
                at = b.get("at") or [0, 0]
                t0 = min(targets, key=lambda t: _hex_distance(
                    at[0], at[1], t[0], t[1]))
                mkey = (turn, ui, t0[0], t0[1])
                if mkey in self._tried_moves:
                    continue
                self._tried_moves.add(mkey)
                r = await self._act(bridge, "move_unit", {
                    "unit_index": ui, "target_x": t0[0], "target_y": t0[1]},
                    turn)
                self.journal.add("action", {
                    "tool": "builder", "args": {"unit_index": ui,
                                                "moving_to": list(t0)},
                    "result": str(r)[:120]}, turn=turn)

    # ── monitored end_turn ────────────────────────────────────────

    async def _end_turn_once(self, bridge: Bridge, before) -> str:
        """Fire one end_turn call and monitor the turn state while it runs.
        Returns the bridge narration when it arrives (or an early-exit note)."""
        timeout = max(30.0, float(self.cfg.autopilot.end_turn_http_timeout_s))
        et = asyncio.create_task(asyncio.to_thread(
            bridge.act, "end_turn", {"fast": True}, timeout))

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
                # turn advanced — give the narration a short grace so the
                # journal keeps its "== Events ==" detail, but never block the
                # next cycle on it: responsiveness beats narration completeness.
                grace = time.monotonic() + float(
                    self.cfg.autopilot.narration_grace_s)
                while time.monotonic() < grace and not et.done():
                    await asyncio.sleep(0.25)
                if et.done():
                    try:
                        return str(et.result())
                    except Exception as e:  # noqa: BLE001
                        return f"Error: {e}"
                return f"Turn advanced to {qs['turn']} (narration not harvested)"
        return "Error: stopped"

    async def _ask_judge_inline(self, questions: dict, turn,
                                situation: str) -> dict | None:
        """One-off judge call outside the normal gate cycle (used by the
        end-turn blocker handlers for deals/diplomacy). Returns the answers
        dict, or None when the judge failed — callers must have a fallback."""
        model = self.cfg.llm.model or None
        state = {"situation": situation, "empire": {}, "threats": []}
        try:
            resp, latency_ms = await asyncio.to_thread(
                self.jev_fn, state, questions, model)
        except Exception as e:  # noqa: BLE001
            log.warning("inline judge call failed: %s", e)
            return None
        answers = (resp or {}).get("answers") or {}
        self.journal.add("jev", {
            "request": {"state": state, "questions": questions,
                        "model": model or self.cfg.engine_summary()},
            "response": resp, "error": None, "latency_ms": latency_ms,
            "meta": {"title": f"blocker T{turn} 外交判断"},
        }, turn=turn)
        return answers if isinstance(answers, dict) else None

    async def _handle_blocker(self, bridge: Bridge, kind: str, raw: str,
                              turn) -> str:
        """Best-effort resolution of a known end_turn blocker.
        Returns 'handled' or 'unhandleable'."""
        self._set(blocker=kind)
        if kind == "world_congress":
            # act_data returns the parsed payload dict; _act would stringify it.
            wc = await asyncio.to_thread(bridge.act_data,
                                         "get_world_congress", {})
            self.journal.add("action", {
                "tool": "wc_note",
                "args": {},
                "result": ("world congress requires votes — registering the "
                           "bridge's default strategy (option A, spread favor; "
                           "free votes still counted)"),
            }, turn=turn)
            r = await self._act(bridge, "queue_wc_votes", {"votes": []}, turn)
            if r.startswith("Error"):
                return "unhandleable"
            # An OPEN session (is_in_session) blocks turn processing even with
            # no resolutions to pick — queueing the voter alone leaves it up.
            if isinstance(wc, dict) and wc.get("is_in_session"):
                r2 = await self._act(bridge, "submit_congress", {}, turn)
                if r2.startswith("Error"):
                    return "unhandleable"
            return "handled"
        if kind == "trade_deal":
            # Jev decides per deal (friendly delegations/borders/fair trades
            # are worth accepting); blind auto-decline only as fallback.
            deals = await asyncio.to_thread(bridge.act_data,
                                            "get_pending_deals", {})
            decided_any = False
            if isinstance(deals, list) and deals:
                for d in deals[:3]:
                    if not isinstance(d, dict):
                        continue
                    pid = d.get("other_player_id")
                    if pid is None:
                        continue
                    them = "; ".join(
                        f"{i.get('name')} x{i.get('amount')}"
                        + (" per turn" if (i.get("duration") or 0) > 0 else "")
                        for i in (d.get("items_from_them") or [])
                        if isinstance(i, dict)) or "nothing"
                    us = "; ".join(
                        f"{i.get('name')} x{i.get('amount')}"
                        + (" per turn" if (i.get("duration") or 0) > 0 else "")
                        for i in (d.get("items_from_us") or [])
                        if isinstance(i, dict)) or "nothing"
                    strategy = (self._strategy or {}).get("path")
                    accept = False
                    if self.cfg.autopilot.auto_decline_deals:
                        ans = await self._ask_judge_inline(
                            {"deal_response": {
                                "type": "choice",
                                "instructions": (
                                    f"{d.get('other_player_name')} offers a "
                                    f"trade: they give [{them}], we give "
                                    f"[{us}]. Accept? Delegations, embassies, "
                                    f"open borders and fair gold trades are "
                                    f"usually worth accepting; decline "
                                    f"lopsided or suspiciously generous deals."
                                    + (f" (Empire strategy: {strategy} victory.)"
                                       if strategy else "")),
                                "criteria": {
                                    "accept": "Accept the deal",
                                    "decline": "Decline the deal"},
                            }}, turn,
                            situation=f"Trade offer from {d.get('other_player_name')}")
                        accept = bool(
                            (ans or {}).get("deal_response", {})
                            .get("choice") == "accept")
                    r = await self._act(bridge, "respond_to_deal",
                                        {"other_player_id": int(pid),
                                         "accept": accept}, turn)
                    decided_any = True
                    self.journal.add("action", {
                        "tool": "deal_response", "args": {
                            "other_player_id": pid, "accept": accept},
                        "result": str(r)[:150]}, turn=turn)
                if decided_any:
                    return "handled"
            # fallback: old narration-based blind decline
            if not self.cfg.autopilot.auto_decline_deals:
                return "unhandleable"
            ids = re.findall(r"other_player_id[^0-9-]*(-?\d+)",
                             str(raw))
            if not ids:
                return "unhandleable"
            for pid in dict.fromkeys(ids):
                await self._act(bridge, "respond_to_deal",
                                {"other_player_id": int(pid), "accept": False}, turn)
            return "handled"
        if kind == "diplomacy":
            # Jev decides when the session offers a real choice (friendship,
            # deals, demands); plain goodbye screens just close.
            sessions = await asyncio.to_thread(bridge.act_data,
                                               "get_diplomacy_sessions", {})
            decided_any = False
            if isinstance(sessions, list) and sessions:
                for s in sessions[:3]:
                    if not isinstance(s, dict):
                        continue
                    pid = s.get("other_player_id")
                    if pid is None:
                        continue
                    keys = [c.get("key") for c in (s.get("choices") or [])
                            if isinstance(c, dict) and c.get("key")]
                    real = [k for k in keys
                            if "EXIT" not in str(k).upper()]
                    dialogue = str(s.get("dialogue_text") or "").strip()
                    if real:
                        # enumerated choices — Jev picks one
                        ans = await self._ask_judge_inline(
                            {"diplomacy_response": {
                                "type": "choice",
                                "instructions": (
                                    f"{s.get('other_civ_name')} "
                                    f"({s.get('other_leader_name')}) opens "
                                    f"diplomacy: \"{dialogue[:180]}\" "
                                    f"— choose a response; EXIT closes "
                                    f"without committing."),
                                "criteria": {k: f"respond {k}"
                                             for k in real + ["EXIT"]},
                            }}, turn,
                            situation=f"Diplomacy with {s.get('other_civ_name')}")
                        choice = ((ans or {}).get("diplomacy_response", {})
                                  .get("choice")) or "EXIT"
                        r = await self._act(bridge, "diplomacy_respond", {
                            "other_player_id": int(pid),
                            "response": str(choice)}, turn)
                    elif dialogue:
                        # QUESTION session (no enumerated choices, e.g. an
                        # embassy request) — EXIT won't close it; answer it.
                        ans = await self._ask_judge_inline(
                            {"diplomacy_response": {
                                "type": "choice",
                                "instructions": (
                                    f"{s.get('other_civ_name')} asks: "
                                    f"\"{dialogue[:180]}\" Accept or "
                                    f"decline? (embassies/open borders are "
                                    f"usually cheap goodwill; decline "
                                    f"demanding or suspicious requests.)"),
                                "criteria": {
                                    "ACCEPT": "Accept the request",
                                    "DECLINE": "Decline the request"},
                            }}, turn,
                            situation=f"Proposal from {s.get('other_civ_name')}")
                        accept = ((ans or {}).get("diplomacy_response", {})
                                  .get("choice")) != "DECLINE"
                        # fallback chain until the session actually closes
                        r = "Error: unhandled"
                        for tool, args in (
                            ("respond_to_deal",
                             {"other_player_id": int(pid), "accept": accept}),
                            ("diplomacy_respond",
                             {"other_player_id": int(pid),
                              "response": "ACCEPT" if accept else "DECLINE"}),
                            ("diplomacy_respond",
                             {"other_player_id": int(pid),
                              "response": "CHOICE_POSITIVE" if accept
                                          else "CHOICE_NEGATIVE"}),
                            ("diplomacy_respond",
                             {"other_player_id": int(pid),
                              "response": "EXIT"}),
                        ):
                            r = await self._act(bridge, tool, args, turn)
                            if "Error" not in r:
                                await asyncio.sleep(1.2)
                                left = await asyncio.to_thread(
                                    bridge.act_data,
                                    "get_diplomacy_sessions", {})
                                still = any(
                                    (isinstance(x, dict)
                                     and x.get("other_player_id") == pid)
                                    for x in (left or []))
                                if not still:
                                    break
                    else:
                        choice = "EXIT"
                        r = await self._act(bridge, "diplomacy_respond", {
                            "other_player_id": int(pid),
                            "response": "EXIT"}, turn)
                    decided_any = True
                    self.journal.add("action", {
                        "tool": "diplomacy_response", "args": {
                            "other_player_id": pid,
                            "dialogue": dialogue[:80]},
                        "result": str(r)[:150]}, turn=turn)
                if decided_any:
                    return "handled"
            ids = re.findall(r"other_player_id[^0-9-]*(-?\d+)", str(raw))
            if not ids:
                return "unhandleable"
            for pid in dict.fromkeys(ids):
                await self._act(bridge, "diplomacy_respond",
                                {"other_player_id": int(pid), "response": "EXIT"}, turn)
            return "handled"
        if kind == "dedication":
            # Era dedication (commemoration): a mandatory strategic pick that
            # only closes when a dedication is chosen — Jev decides per age
            # type (dark/golden/normal change the bonuses).
            ded = await asyncio.to_thread(bridge.act_data,
                                          "get_dedications", {})
            choices = [c for c in ((ded or {}).get("choices") or [])
                       if isinstance(c, dict) and c.get("index") is not None]
            if not choices:
                self.journal.add("action", {
                    "tool": "dedication_debug", "args": {},
                    "result": json.dumps(ded, ensure_ascii=False,
                                         default=str)[:600]}, turn=turn)
                return "unhandleable"
            age = str((ded or {}).get("age_type") or "Normal").lower()
            desc_key = {"dark": "dark_desc", "golden": "golden_desc",
                        "heroic": "golden_desc"}.get(age, "normal_desc")
            strategy = (self._strategy or {}).get("path")
            pick = None
            ans = await self._ask_judge_inline(
                {"dedication_pick": {
                    "type": "choice",
                    "instructions": (
                        f"A new era begins ({age} age) — pick our "
                        f"DEDICATION (era bonus) for the coming era."
                        + (f" Empire strategy: {strategy} victory."
                           if strategy else "")),
                    "criteria": {
                        str(c["index"]): (
                            f"{c.get('name', '?')} — "
                            f"{c.get(desc_key) or c.get('normal_desc') or ''}")
                        for c in choices
                    },
                }}, turn,
                situation=f"Era dedication choice ({age} age)")
            pick = ((ans or {}).get("dedication_pick", {}).get("choice"))
            if pick is None:
                pick = str(choices[0]["index"])  # deterministic fallback
            r = await self._act(bridge, "choose_dedication",
                                {"dedication_index": int(pick)}, turn)
            self.journal.add("action", {
                "tool": "choose_dedication",
                "args": {"dedication_index": int(pick)},
                "result": str(r)[:150]}, turn=turn)
            return "handled" if "Error" not in r else "unhandleable"
        if kind == "great_person":
            # Unclaimed Great Person blocks the turn — Jev picks the best
            # claim (ability text + strategy context; religion path strongly
            # favours Great Prophets). Fallback: claim the first available.
            gp_list = await asyncio.to_thread(bridge.act_data,
                                              "get_great_people", {})
            claimable = [g for g in (gp_list or [])
                         if isinstance(g, dict)
                         and g.get("can_recruit") and g.get("individual_id")]
            if not claimable:
                # nothing recruitable with points — the popup may be a
                # patronage offer; reject to unblock
                if isinstance(gp_list, list) and gp_list:
                    g0 = gp_list[0]
                    if g0.get("individual_id") is not None:
                        await self._act(bridge, "reject_great_person",
                                        {"individual_id": g0["individual_id"]},
                                        turn)
                        return "handled"
                return "unhandleable"
            strategy = (self._strategy or {}).get("path")
            pick = None
            ans = await self._ask_judge_inline(
                {"great_person_pick": {
                    "type": "choice",
                    "instructions": (
                        "We may CLAIM a Great Person right now — pick which. "
                        "Judge each by its ability and our needs."
                        + (f" Empire strategy: {strategy} victory — Great "
                           f"Prophets are CRITICAL for founding a religion."
                           if strategy == "religion" else "")),
                    "criteria": {
                        str(g["individual_id"]): (
                            f"{g.get('class_name', '?')} "
                            f"{g.get('individual_name', '?')} "
                            f"({g.get('era_name', '?')}) — "
                            f"{str(g.get('ability') or '')[:120]}")
                        for g in claimable
                    },
                }}, turn,
                situation="Great Person claim available")
            pick = ((ans or {}).get("great_person_pick", {}).get("choice"))
            if pick is None:
                pick = str(claimable[0]["individual_id"])
            r = await self._act(bridge, "recruit_great_person",
                                {"individual_id": int(pick)}, turn)
            self.journal.add("action", {
                "tool": "recruit_great_person",
                "args": {"individual_id": int(pick)},
                "result": str(r)[:150]}, turn=turn)
            return "handled" if "Error" not in r else "unhandleable"
        if kind == "policy_fill":
            # Empty government slots block the turn — fill every empty slot
            # (Jev picks per slot; deterministic first-compatible fallback).
            gov = await asyncio.to_thread(bridge.act_data,
                                          "get_policies", {})
            slots = (gov or {}).get("slots") or []
            avail = (gov or {}).get("available_policies") or []
            empty = [s for s in slots
                     if isinstance(s, dict) and not s.get("current_policy")]
            if not empty:
                # Stale preflight flag — a prior async fill landed between the
                # end_turn gate and here. The goal (no empty slots) is already
                # met, so report handled: the end_turn retry re-preflights and
                # self-corrects. Pausing here stranded the autopilot on a
                # problem that no longer existed.
                return "handled"
            questions: dict = {}
            fallback: dict = {}
            for s in empty:
                sid = str(s.get("slot_index"))
                opts = _policy_options(s.get("slot_type") or "", avail)
                if not opts:
                    continue
                fallback[sid] = next(iter(opts))
                questions[f"policy_pick:{sid}"] = {
                    "type": "choice",
                    "slot_index": s.get("slot_index"),
                    "instructions": (
                        f"The {str(s.get('slot_type') or '').replace('SLOT_', '')} "
                        f"policy slot is EMPTY and the game refuses to end the "
                        f"turn until filled. Pick a policy."
                        + (f" Empire strategy: "
                           f"{(self._strategy or {}).get('path')} victory."
                           if (self._strategy or {}).get("path") else "")),
                    "criteria": opts,
                }
            if not questions:
                return "unhandleable"
            assignments = dict(fallback)
            ans = await self._ask_judge_inline(questions, turn,
                                               situation="Policy slots must "
                                                         "be filled")
            if ans:
                for qid in questions:
                    c = (ans.get(qid) or {}).get("choice")
                    if c:
                        assignments[qid.split(":", 1)[1]] = str(c)
            r = await self._act(bridge, "set_policies",
                                {"assignments": assignments}, turn)
            self.journal.add("action", {
                "tool": "policy_fill", "args": {"assignments": assignments},
                "result": str(r)[:150]}, turn=turn)
            if "Error" not in r:
                # policy changes are async — let the game settle so a stale
                # preflight flag doesn't re-trigger the fill next retry
                await asyncio.sleep(1.5)
                self._last_policy_review = turn
                self._save_state()
                return "handled"
            return "unhandleable"
        if kind == "governor":
            # Unspent governor titles block the turn — appoint (Jev picks
            # which) or promote an appointed governor. Deterministic
            # fallbacks keep the turn moving if the judge fails.
            gov = await asyncio.to_thread(bridge.act_data,
                                          "get_governors", {})
            strategy = (self._strategy or {}).get("path")
            hint = (f" Empire strategy: {strategy} victory."
                    if strategy else "")
            snap = self.status.get("last_snapshot") or {}
            cities = [c for c in (snap.get("cities") or [])
                      if c.get("city_id") is not None]

            # rescue first: an appointed-but-unassigned governor keeps the
            # screen open — assign to the capital/largest city
            for g in ((gov or {}).get("appointed") or []):
                if (isinstance(g, dict)
                        and g.get("assigned_city_id", -1) in (-1, None)
                        and cities):
                    await self._act(bridge, "assign_governor", {
                        "governor_type": g.get("governor_type"),
                        "city_id": cities[0]["city_id"]}, turn)
                    await asyncio.sleep(1.0)
            to_appoint = [g for g in ((gov or {}).get("available_to_appoint")
                                      or []) if isinstance(g, dict)]
            if (gov or {}).get("can_appoint") and to_appoint:
                ans = await self._ask_judge_inline(
                    {"governor_appoint": {
                        "type": "choice",
                        "instructions": (
                            "We must APPOINT a Governor (a title is burning "
                            "a hole in the empire's pocket). Pick the one "
                            "that best serves our needs." + hint),
                        "criteria": {
                            g.get("governor_type"): (
                                f"{g.get('name')} ({g.get('title')}) — "
                                f"{str(g.get('base_ability_desc') or g.get('base_ability') or '')[:110]}")
                            for g in to_appoint if g.get("governor_type")
                        },
                    }}, turn, situation="Governor appointment")
                pick = ((ans or {}).get("governor_appoint", {})
                        .get("choice")) or to_appoint[0].get("governor_type")
                r = await self._act(bridge, "appoint_governor",
                                    {"governor_type": str(pick)}, turn)
                self.journal.add("action", {
                    "tool": "appoint_governor",
                    "args": {"governor_type": str(pick)},
                    "result": str(r)[:150]}, turn=turn)
                if "Error" not in r:
                    # appoint is async AND the screen stays open until the
                    # governor is assigned to a city — station at the capital
                    if cities:
                        await asyncio.sleep(1.0)
                        await self._act(bridge, "assign_governor", {
                            "governor_type": str(pick),
                            "city_id": cities[0]["city_id"]}, turn)
                    await asyncio.sleep(1.0)
                    return "handled"
                return "unhandleable"
            # all appointed but titles remain → promote
            appointed = [g for g in ((gov or {}).get("appointed") or [])
                         if isinstance(g, dict)]
            for g in appointed:
                promos = [p for p in (g.get("available_promotions") or [])
                          if isinstance(p, dict) and p.get("promotion_type")]
                if not promos:
                    continue
                ans = await self._ask_judge_inline(
                    {"governor_promote": {
                        "type": "choice",
                        "instructions": (
                            f"Promote Governor {g.get('name')} "
                            f"(currently at {g.get('assigned_city_name')}) — "
                            f"pick the promotion." + hint),
                        "criteria": {
                            p.get("promotion_type"): (
                                f"{p.get('name')} — "
                                f"{str(p.get('description') or '')[:110]}")
                            for p in promos
                        },
                    }}, turn, situation="Governor promotion")
                pick = ((ans or {}).get("governor_promote", {})
                        .get("choice")) or promos[0].get("promotion_type")
                r = await self._act(bridge, "promote_governor", {
                    "governor_type": g.get("governor_type"),
                    "promotion_type": str(pick)}, turn)
                self.journal.add("action", {
                    "tool": "promote_governor",
                    "args": {"governor_type": g.get("governor_type"),
                             "promotion_type": str(pick)},
                    "result": str(r)[:150]}, turn=turn)
                if "Error" not in r:
                    await asyncio.sleep(1.5)
                    return "handled"
                if "No governor points available" in r:
                    # points were consumed by a prior async appoint/promote —
                    # the stale preflight flag was the only problem
                    return "handled"
                return "unhandleable"
            return "unhandleable"
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
        consecutive_timeouts = 0
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
                consecutive_timeouts = 0 if kind != "timeout" else consecutive_timeouts + 1
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
                    self._set(step="ending_turn", blocker=None)
                    continue
                if kind in ("timeout", "error"):
                    if kind == "error" and not await asyncio.to_thread(bridge.healthy):
                        raise ConnectionError(f"end_turn transport failed: {text[:150]}")
                    # Repeated HTTP timeouts with no turn advance usually mean
                    # the bridge's end_turn is stuck in its long internal wait
                    # (e.g. units still have moves → game refuses to advance)
                    # and its blocker narration will never reach us in time.
                    # Unblock proactively instead of burning a full 900s budget.
                    if (consecutive_timeouts >= max(1, int(ap.timeout_blocker_after))
                            and total_handled <= BLOCKER_TOTAL_CAP):
                        total_handled += 1
                        consecutive_timeouts = 0
                        # A between-turns World Congress session also manifests
                        # as "still processing" with no dismissable popup —
                        # probe it explicitly before generic unblocking.
                        wc = None
                        try:
                            wc = await asyncio.to_thread(
                                bridge.act_data, "get_world_congress", {})
                        except Exception:
                            wc = None
                        if isinstance(wc, dict) and wc.get("is_in_session"):
                            self.journal.add("action", {
                                "tool": "timeout_unblock", "args": {},
                                "result": ("end_turn stalled by an open world "
                                           "congress session — routing to the "
                                           "world_congress handler"),
                            }, turn=before)
                            self._set(step="handling_world_congress")
                            status_ = await self._handle_blocker(
                                bridge, "world_congress", text, before)
                            if status_ != "handled":
                                self._pause("world congress session not resolvable")
                                return
                            self._set(step="ending_turn")
                            await asyncio.sleep(1.0)
                            continue
                        # Session-less AI proposals (e.g. a peace offer) also
                        # wedge turn processing with Lua-level timeouts — probe
                        # pending deals explicitly before generic unblocking.
                        try:
                            pend = await asyncio.to_thread(
                                bridge.act_data, "get_pending_deals", {})
                        except Exception:
                            pend = None
                        if isinstance(pend, list) and pend:
                            self.journal.add("action", {
                                "tool": "timeout_unblock", "args": {},
                                "result": (f"end_turn stalled by {len(pend)} "
                                           "pending AI deal(s) — routing to the "
                                           "trade_deal handler"),
                            }, turn=before)
                            self._set(step="handling_trade_deal")
                            status_ = await self._handle_blocker(
                                bridge, "trade_deal", text, before)
                            if status_ != "handled":
                                self._pause("pending deal not resolvable")
                                return
                            self._set(step="ending_turn")
                            await asyncio.sleep(1.0)
                            continue
                        self._set(step="handling_timeout_blocker")
                        self.journal.add("action", {
                            "tool": "timeout_unblock", "args": {
                                "after_timeouts": max(1, int(ap.timeout_blocker_after))},
                            "result": ("end_turn timed out repeatedly with no turn "
                                       "advance — dismissing popup and skipping "
                                       "remaining unit moves")},
                            turn=before)
                        await self._act(bridge, "dismiss_popup", {}, before)
                        await self._act(bridge, "skip_remaining_units", {}, before)
                        self._set(step="ending_turn")
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
