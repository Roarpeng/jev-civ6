"""Autopilot: the Jev-driven play loop, bridged.

Architecture (the bridge insight): Civ 6's FireTuner link is single-client
and temperamental about reconnects. The civ6-mcp server (upstream, battle-
tested) is the ONE process that owns that link — its embedded web API on
port 8000 exposes reads, and (with our web_api.py patch) a whitelisted
/api/action endpoint. The autopilot never touches FireTuner itself:

    auto mode → [one-time takeover] → spawn `python -m civ_mcp` (bridge)
              → cycle: HTTP reads → decision gate → Jev → HTTP actions
    manual mode → bridge process dies with the loop

The bridge inherits upstream's connection care: persistent link, in-band
auto-reconnect, PopupWatcher dismissing modals. We manage zero sockets.
"""
import asyncio
import json
import logging
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable

from .decision_gate import _is_idle

log = logging.getLogger("jevciv6.autopilot")

FAIL_LIMIT = 3
GAME_PORT = 4318
BRIDGE_URL = os.environ.get("JEVCIV6_BRIDGE_URL", "http://127.0.0.1:8000")
BRIDGE_READY_TIMEOUT = 90  # seconds to wait for /api/overview after spawn
SENTINEL_INTERVAL = 1.0    # always-on idle-signal probe cadence
SENTINEL_COOLDOWN = 6.0    # min seconds between sentinel-triggered decides
                           # (lets executed choices register in game state)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _takeover_once() -> str:
    """Kill any other python process holding the FireTuner link, so the
    bridge we are about to spawn can claim it. Excludes our own process."""
    ps = ("Get-NetTCPConnection -RemotePort %d -State Established "
          "-ErrorAction SilentlyContinue | "
          "Select-Object -ExpandProperty OwningProcess -Unique" % GAME_PORT)
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


class Bridge:
    """Owns the civ6-mcp bridge subprocess and its HTTP surface."""

    def __init__(self, journal):
        self.journal = journal
        self.proc: subprocess.Popen | None = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def healthy(self) -> bool:
        """True when the bridge answers HTTP (link up, game connected)."""
        if not self.alive():
            return False
        return "error" not in _http("GET", "/api/overview", timeout=8.0)

    async def start(self, turn=None) -> None:
        report = await asyncio.to_thread(_takeover_once)
        self.journal.add("action", {
            "tool": "bridge_takeover", "args": {},
            "result": report,
        }, turn=turn)
        # stdin must stay OPEN (PIPE, never written) or the stdio MCP server
        # sees EOF and exits; stdout/stderr discarded so pipes never fill.
        self.proc = subprocess.Popen(
            [os.environ.get("JEVCIV6_PYTHON", "python"), "-m", "civ_mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        deadline = asyncio.get_event_loop().time() + BRIDGE_READY_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"bridge process exited rc={self.proc.returncode} "
                    "(python/env problem — is civ6-mcp installed?)")
            if self.healthy():
                self.journal.add("action", {
                    "tool": "bridge_up", "args": {"pid": self.proc.pid},
                    "result": f"bridge ready at {BRIDGE_URL}",
                }, turn=turn)
                return
            await asyncio.sleep(2)
        raise RuntimeError(f"bridge not ready after {BRIDGE_READY_TIMEOUT}s")

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


def build_snapshot(ov: dict, units: list, cities: list, tech: dict,
                   threats: list, prod_options: list | None = None) -> dict:
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
             "pop": c.get("population"), "growth": f"{c.get('turns_to_grow')}t",
             "production": (str(c.get("currently_building"))
                            if not _is_building_idle(c.get("currently_building"))
                            else "")}
            for c in cities
        ],
        "units": [
            {"unit_index": u.get("unit_index"), "type": u.get("unit_type"),
             "at": [u.get("x"), u.get("y")], "cs": u.get("combat_strength"),
             "moves": u.get("moves_remaining")}
            for u in units
        ],
        "threats": [
            {"type": t.get("unit_type"), "at": [t.get("x"), t.get("y")],
             "cs": t.get("combat_strength"), "distance": t.get("distance"),
             "owner": t.get("owner_name")}
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
            "production_options": [
                {"id": p.get("item_name"),
                 "desc": f"{p.get('category')} · {p.get('turns')}t"}
                for p in (prod_options or [])
            ],
        },
        "idle_settler_count": len(idle_settlers),
    }


class AutoPilot:
    """Runs gate→Jev→HTTP-actions→end_turn cycles through the bridge."""

    def __init__(self, journal, gate_fn, jev_fn):
        self.journal = journal
        self.gate_fn = gate_fn
        self.jev_fn = jev_fn  # sync; run in a thread
        self._stop_flag = False
        self._decide_lock = asyncio.Lock()
        self._sentinel_hot = 0.0       # monotonic time of last sentinel decide
        self._in_turn_advance = False
        self.on_pause: Callable[[], None] | None = None
        self.task: asyncio.Task | None = None
        self.sentinel_task: asyncio.Task | None = None
        self.status: dict = {
            "running": False, "step": "idle", "turn": None, "ticks": 0,
            "cycles": 0, "last_error": None, "started_at": None,
            "connected": False, "bridge_pid": None,
        }

    def status_public(self) -> dict:
        return dict(self.status)

    def _set(self, **kw) -> None:
        self.status.update(**kw)

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self._stop_flag = False
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
                           bridge_pid=None)

    def _pause(self) -> None:
        self.status["step"] = "auto_paused"
        self.journal.add("action", {
            "tool": "control_mode", "args": {"mode": "manual"},
            "result": f"auto-paused after {FAIL_LIMIT} consecutive failures",
        }, turn=self.status.get("turn"))
        if self.on_pause:
            try:
                self.on_pause()
            except Exception:  # noqa: BLE001
                pass

    async def _sentinel_loop(self, bridge: "Bridge") -> None:
        """Always-on watcher: probes idle signals every second and triggers
        an immediate decide cycle the moment a choice appears — independent
        of turn boundaries and of end_turn being in flight."""
        while not self._stop_flag:
            await asyncio.sleep(SENTINEL_INTERVAL)
            try:
                if time.monotonic() - self._sentinel_hot < SENTINEL_COOLDOWN:
                    continue
                if self._decide_lock.locked():
                    continue  # a decide is already running
                if await self._needs_decision_fast(bridge):
                    self._sentinel_hot = time.monotonic()
                    self._set(step="sentinel_decision")
                    async with self._decide_lock:
                        await self._decide_cycle(
                            bridge, self.status.get("last_snapshot"))
                    if self._in_turn_advance:
                        self._set(step="ending_turn")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the sentinel never dies
                pass

    async def _loop(self) -> None:
        bridge = Bridge(self.journal)
        prev_snapshot: dict | None = None
        fails = 0
        try:
            try:
                self._set(step="starting bridge")
                await bridge.start(self.status.get("turn"))
                self._set(connected=True, bridge_pid=bridge.proc.pid)
            except Exception as e:  # noqa: BLE001
                self.status["last_error"] = str(e)
                self._pause()
                return
            self.sentinel_task = asyncio.create_task(
                self._sentinel_loop(bridge))
            while not self._stop_flag:
                try:
                    if not bridge.healthy():
                        raise ConnectionError("bridge lost — respawning")
                    async with self._decide_lock:
                        await self._decide_cycle(bridge, prev_snapshot)
                    prev_snapshot = self.status.get("last_snapshot")
                    fails = 0
                    self.status["cycles"] += 1
                    await self._turn_advance(bridge)
                except asyncio.CancelledError:
                    raise
                except ConnectionError as e:
                    fails += 1
                    self.status["last_error"] = str(e)
                    self.journal.add("action", {
                        "tool": "autopilot_error", "args": {"fail": fails},
                        "result": str(e)[:400],
                    }, turn=self.status.get("turn"))
                    if fails >= FAIL_LIMIT:
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
                    if fails >= FAIL_LIMIT:
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
            if self.status["step"] != "auto_paused":
                self.status["step"] = "stopped"
            await bridge.stop(self.status.get("turn"))

    async def _needs_decision_fast(self, bridge: Bridge) -> bool:
        """Cheap idle-signal probe (~2 reads, 8s budget). Used as a sentinel
        while end_turn is in flight so choices react in seconds, not after
        the whole AI-turn wait."""
        try:
            tech, cities_raw = await asyncio.gather(
                asyncio.to_thread(_http, "GET", "/api/tech", None, 8.0),
                asyncio.to_thread(_http, "GET", "/api/cities", None, 8.0),
            )
        except Exception:  # noqa: BLE001
            return False
        if isinstance(tech, dict) and "error" in tech:
            return False
        if _is_idle((tech or {}).get("current_research"),
                    (tech or {}).get("current_research_turns")):
            return True
        if _is_idle((tech or {}).get("current_civic"),
                    (tech or {}).get("current_civic_turns")):
            return True
        cities = cities_raw[0] if isinstance(cities_raw, list) else []
        return any((c.get("currently_building") or "NONE") == "NONE"
                   for c in cities)

    async def _turn_advance(self, bridge: Bridge) -> None:
        """End the turn and wait for the game (AI processing). Choice
        reactions during the wait are handled by the always-on sentinel,
        so this is a plain wait now."""
        self._set(step="ending_turn")
        self._in_turn_advance = True
        et = asyncio.create_task(
            asyncio.to_thread(bridge.act, "end_turn", {}, 180.0))
        try:
            while not et.done() and not self._stop_flag:
                await asyncio.sleep(0.5)
            result = await et
        except asyncio.CancelledError:
            et.cancel()
            raise
        finally:
            self._in_turn_advance = False
        self.journal.add("action", {
            "tool": "end_turn", "args": {"mode": "auto"},
            "result": str(result)[:500],
        }, turn=self.status.get("turn"))
        if "timed out" in str(result) or "Error: HTTP 5" in str(result):
            # timeout = the game is slow (AI turns), not a dead bridge —
            # the turn usually DID advance; verify next cycle instead of
            # killing the bridge and its in-flight end_turn handler
            log.warning("end_turn slow/blocked: %s", str(result)[:200])
        elif "Error" in str(result):
            raise ConnectionError(f"end_turn transport failed: {result[:150]}")
        if "Cannot end turn" in str(result):
            # hard blocker (choice popup etc.) — the sentinel/gate should
            # have made the choice; log and let the next cycle handle it
            log.warning("end_turn blocked: %s", str(result)[:200])
        self.status["ticks"] += 1
        # no sleep — the next decide cycle starts immediately

    async def _decide_cycle(self, bridge: Bridge,
                            prev_snapshot: dict | None) -> None:
        # 1. collect via HTTP reads
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
        cities = cities_raw[0] if isinstance(cities_raw, list) else []

        prod_options = None
        if cities and _is_building_idle(cities[0].get("currently_building")):
            prod_options = await asyncio.to_thread(
                bridge.production_options, cities[0]["city_id"])

        snapshot = build_snapshot(ov, units, cities, tech, threats, prod_options)
        self.status["last_snapshot"] = snapshot
        self._set(turn=snapshot["turn"])
        self.journal.add("state", {"snapshot": snapshot}, turn=snapshot["turn"])

        # 2. gate
        self._set(step="gating")
        gate = self.gate_fn(snapshot, prev_snapshot)
        self.journal.add("gate", {
            "result": {k: gate[k] for k in
                       ("should_ask", "triggers", "question_ids", "skip_reason")},
        }, turn=snapshot["turn"])

        # 3. judge only on decision points
        if gate["should_ask"]:
            self._set(step="asking_jev")
            resp, latency_ms = await asyncio.to_thread(
                self.jev_fn, {"snapshot": snapshot}, gate["questions"])
            self.journal.add("jev", {
                "request": {"state": {"snapshot": snapshot},
                            "questions": gate["questions"],
                            "model": "jev-latest"},
                "response": resp, "error": None, "latency_ms": latency_ms,
                "meta": {"title": f"autopilot T{snapshot['turn']} 定向判断"},
            }, turn=snapshot["turn"])
            # 4. execute via HTTP actions
            self._set(step="executing")
            await self._execute(bridge, resp.get("answers") or {},
                                snapshot, snapshot["turn"])

        # end_turn lives in _turn_advance — decisions never wait on it

    async def _execute(self, bridge: Bridge, answers: dict, snapshot: dict,
                       turn: int) -> None:
        async def act(tool: str, args: dict) -> None:
            result = await asyncio.to_thread(bridge.act, tool, args)
            self.journal.add("action", {"tool": tool, "args": args,
                                        "result": str(result)[:400]},
                             turn=turn)

        idle_city = next((c for c in snapshot["cities"] if not c["production"]),
                         None)
        for qid, ans in answers.items():
            if not isinstance(ans, dict):
                continue
            if qid == "research_pick" and ans.get("choice"):
                await act("set_research", {"tech_name": ans["choice"]})
            elif qid == "civic_pick" and ans.get("choice"):
                await act("set_civic", {"civic_name": ans["choice"]})
            elif qid == "production_pick" and ans.get("choice") and idle_city:
                choice = ans["choice"]
                cat = next((p for p in ("UNIT", "BUILDING", "DISTRICT", "PROJECT")
                            if choice.startswith(p + "_")), None)
                if cat and idle_city.get("city_id") is not None:
                    await act("set_city_production", {
                        "city_id": idle_city["city_id"],
                        "item_type": cat, "item_name": choice,
                        "target_x": None, "target_y": None,
                    })
                else:
                    self.journal.add("action", {
                        "tool": "verdict:production_pick",
                        "args": {"choice": choice},
                        "result": "unrecognized item — not executed"}, turn=turn)
            elif qid == "threat_response" and ans.get("noul") is not None:
                if ans["noul"] >= 0.5:
                    await self._intercept(bridge, snapshot, turn)
                else:
                    self.journal.add("action", {
                        "tool": "threat_response",
                        "args": {"noul": ans["noul"]},
                        "result": "verdict_only — hold current orders"}, turn=turn)
            else:
                self.journal.add("action", {
                    "tool": f"verdict:{qid}",
                    "args": {"answer": ans.get("choice", ans.get("noul"))},
                    "result": "verdict_only — no executor for this question yet"},
                    turn=turn)

    async def _intercept(self, bridge: Bridge, snapshot: dict, turn: int) -> None:
        """Send the strongest melee unit one step toward the nearest threat."""
        threats = snapshot.get("threats") or []
        military = [u for u in snapshot.get("units") or []
                    if any(k in str(u.get("type", "")).upper()
                           for k in ("WARRIOR", "SLINGER", "SWORDSMAN", "SPEARMAN"))]
        if not threats or not military:
            self.journal.add("action", {
                "tool": "threat_response", "args": {},
                "result": "no military unit or no threat position — hold",
            }, turn=turn)
            return
        t = min(threats, key=lambda x: x.get("distance") or 99)
        u = max(military, key=lambda x: x.get("cs") or 0)
        result = await asyncio.to_thread(
            bridge.act, "move_unit",
            {"unit_index": u["unit_index"], "target_x": t["at"][0],
             "target_y": t["at"][1]})
        if "BLOCKED" in str(result):
            result += " | " + await asyncio.to_thread(
                bridge.act, "fortify_unit", {"unit_index": u["unit_index"]})
        self.journal.add("action", {
            "tool": "move_unit",
            "args": {"unit": u["type"], "toward": t["at"]},
            "result": str(result)[:400]}, turn=turn)
