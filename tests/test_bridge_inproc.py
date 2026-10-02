# -*- coding: utf-8 -*-
"""InProcessBridge tests: fakes for GameState/GameConnection, no game needed.

Covers the compatibility contract with the old subprocess/HTTP Bridge:
lifecycle (start/stop/alive/healthy), act/act_data dispatch through the
ACTION_TOOLS whitelist (arg filtering, unknown tools, error containment),
reconnect-on-lost-link, the GET-equivalent reads, and the batched
warroom_collect section plumbing (parsers mocked — their Lua line formats
are civ6-mcp's business, not the bridge's).
"""
import asyncio
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.bridge_inproc import ACTION_TOOLS, InProcessBridge  # noqa: E402


class _BridgeCfgStub:
    game_port = 4318
    ready_timeout_s = 10.0


class _CfgStub:
    bridge = _BridgeCfgStub()


class _Journal:
    def __init__(self):
        self.entries = []

    def add(self, kind, payload, turn=None):
        self.entries.append((kind, payload, turn))


class FakeConnection:
    """Minimal GameConnection stand-in: connect flips the flags, execute_*
    return canned section lines, everything records."""

    def __init__(self):
        self.connected = False
        self.disconnected = False
        self.lua_states = {0: "GameCore_Tuner", 1: "InGame"}
        self.gamecore_index = None
        self.ingame_index = None
        self.connect_calls = 0
        self.ensure_calls = 0
        self.write_luas = []
        self.read_luas = []
        self.write_lines = [
            "SECTION|wr", "WRLINE",
            "SECTION|overview", "OVL",
            "SECTION|units", "UL",
            "SECTION|cities", "CL",
        ]
        self.read_lines = [
            "SECTION|tech", "TL",
            "SECTION|threats", "THL",
        ]

    @property
    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connect_calls += 1
        self.connected = True
        self.gamecore_index = 0
        self.ingame_index = 1

    async def _ensure_game_states(self):
        self.ensure_calls += 1
        if self.gamecore_index is None or self.ingame_index is None:
            raise ConnectionError("GameCore_Tuner/InGame states not found")

    async def disconnect(self):
        self.disconnected = True
        self.connected = False
        self.gamecore_index = None
        self.ingame_index = None

    async def execute_write(self, lua_code, timeout=5.0):
        self.write_luas.append(lua_code)
        return list(self.write_lines)

    async def execute_read(self, lua_code, timeout=5.0):
        self.read_luas.append(lua_code)
        return list(self.read_lines)


class FakeGameState:
    """Records every call; mirrors the GameState method surface the bridge
    dispatches to (async methods, str narrations or structured data)."""

    def __init__(self, conn):
        self.conn = conn
        self.calls = []  # [(method_name, kwargs), ...]

    def _record(self, name, **kwargs):
        self.calls.append((name, kwargs))

    async def set_policies(self, assignments):
        self._record("set_policies", assignments=assignments)
        return f"policies set ({len(assignments)} slot(s))"

    async def get_policies(self):
        self._record("get_policies")
        return {"government_name": "Chiefdom",
                "slots": [{"slot_index": 1, "current_policy": "POLICY_X"}]}

    async def end_turn(self, fast=False):
        self._record("end_turn", fast=fast)
        return "Turn 5 -> 6 | fast end_turn — narration/autosave skipped"

    async def move_unit(self, **kwargs):
        self._record("move_unit", **kwargs)
        return "moved"

    async def set_research(self, tech_name):
        self._record("set_research", tech_name=tech_name)
        return "research set"

    async def improve_tile(self, unit_index, improvement_name):
        self._record("improve_tile", unit_index=unit_index,
                     improvement_name=improvement_name)
        raise RuntimeError("tuner exploded")

    async def get_units(self):
        self._record("get_units")
        return [{"unit_index": 1, "unit_type": "UNIT_SETTLER"}]

    async def get_quick_state(self):
        self._record("get_quick_state")
        return {"turn": 5}

    async def list_city_production(self, city_id):
        self._record("list_city_production", city_id=city_id)
        return [{"item_name": "UNIT_WARRIOR", "category": "UNIT", "turns": 3}]


def _make_bridge(journal=None, takeover_fn=None):
    holder = {}

    def conn_factory():
        conn = FakeConnection()
        holder["conn"] = conn
        return conn

    def gs_factory(conn):
        gs = FakeGameState(conn)
        holder["gs"] = gs
        return gs

    bridge = InProcessBridge(journal, cfg=_CfgStub(),
                             gs_factory=gs_factory,
                             conn_factory=conn_factory,
                             takeover_fn=takeover_fn)
    return bridge, holder


class LifecycleTests(unittest.TestCase):
    def test_start_alive_and_healthy(self):
        bridge, holder = _make_bridge()
        self.assertFalse(bridge.alive())
        asyncio.run(bridge.start())
        try:
            self.assertTrue(bridge.alive())
            # fake conn reports connected + an InGame state index
            self.assertTrue(bridge.healthy())
            self.assertEqual(holder["conn"].connect_calls, 1)
            self.assertGreaterEqual(holder["conn"].ensure_calls, 1)
            # indexes gone (e.g. back at main menu) → not healthy
            holder["conn"].ingame_index = None
            holder["conn"].gamecore_index = None
            self.assertFalse(bridge.healthy())
        finally:
            asyncio.run(bridge.stop())

    def test_stop_releases_and_journals(self):
        journal = _Journal()
        bridge, holder = _make_bridge(journal)
        asyncio.run(bridge.start())
        conn = holder["conn"]
        asyncio.run(bridge.stop())
        self.assertFalse(bridge.alive())
        self.assertFalse(bridge.healthy())
        self.assertTrue(conn.disconnected)
        kinds = [(k, p.get("tool")) for k, p, _t in journal.entries]
        self.assertIn(("action", "bridge_up"), kinds)
        # takeover_fn is None by default → no takeover probe journaled
        self.assertNotIn(("action", "bridge_takeover"), kinds)
        down = [p for k, p, _t in journal.entries if p.get("tool") == "bridge_down"]
        self.assertEqual(len(down), 1)
        self.assertEqual(down[0]["result"],
                         "in-process bridge stopped — FireTuner slot released")
        # stopping twice is a safe no-op
        asyncio.run(bridge.stop())
        self.assertFalse(bridge.alive())

    def test_takeover_fn_is_called(self):
        seen = []

        def takeover(cfg):
            seen.append(cfg)
            return "no competing controller"

        journal = _Journal()
        bridge, _ = _make_bridge(journal, takeover_fn=takeover)
        asyncio.run(bridge.start())
        try:
            self.assertEqual(len(seen), 1)
            self.assertIs(seen[0], bridge.cfg)
            take = [p for k, p, _t in journal.entries
                    if p.get("tool") == "bridge_takeover"]
            self.assertEqual(take[0]["result"], "no competing controller")
        finally:
            asyncio.run(bridge.stop())


class DispatchTests(unittest.TestCase):
    def test_act_set_policies_reaches_fake(self):
        bridge, holder = _make_bridge()
        asyncio.run(bridge.start())
        try:
            r = bridge.act("set_policies", {"assignments": {"1": "POLICY_X"}})
            self.assertEqual(r, "policies set (1 slot(s))")
            gs = holder["gs"]
            # reached as an async GameState call, args untouched
            # (int-key coercion happens inside the real GameState)
            self.assertEqual(gs.calls,
                             [("set_policies", {"assignments": {"1": "POLICY_X"}})])
        finally:
            asyncio.run(bridge.stop())

    def test_act_data_returns_structured_dict(self):
        bridge, holder = _make_bridge()
        asyncio.run(bridge.start())
        try:
            data = bridge.act_data("get_policies", {})
            self.assertEqual(data, {
                "government_name": "Chiefdom",
                "slots": [{"slot_index": 1, "current_policy": "POLICY_X"}],
            })
            self.assertEqual(holder["gs"].calls, [("get_policies", {})])
        finally:
            asyncio.run(bridge.stop())

    def test_unknown_tool(self):
        bridge, _ = _make_bridge()
        asyncio.run(bridge.start())
        try:
            r = bridge.act("definitely_not_a_tool", {})
            self.assertTrue(r.startswith("Error: unknown tool"),
                            msg=r)
            self.assertIn("allowed", r)
            d = bridge.act_data("also_fake", {})
            self.assertIn("error", d)
            self.assertTrue(str(d["error"]).startswith("unknown tool"))
        finally:
            asyncio.run(bridge.stop())

    def test_arg_filtering_per_whitelist(self):
        bridge, holder = _make_bridge()
        asyncio.run(bridge.start())
        try:
            # extra keys are dropped, whitelisted keys survive
            r = bridge.act("move_unit", {"unit_index": 3, "target_x": 1,
                                         "target_y": 2, "surprise": True})
            self.assertEqual(r, "moved")
            self.assertEqual(holder["gs"].calls, [(
                "move_unit",
                {"unit_index": 3, "target_x": 1, "target_y": 2})])
            # None values are dropped too (endpoint semantics)
            holder["gs"].calls.clear()
            bridge.act("set_research", {"tech_name": "TECH_POTTERY",
                                        "civic_name": None})
            self.assertEqual(holder["gs"].calls, [(
                "set_research", {"tech_name": "TECH_POTTERY"})])
        finally:
            asyncio.run(bridge.stop())

    def test_act_error_contained(self):
        bridge, _ = _make_bridge()
        asyncio.run(bridge.start())
        try:
            # the fake's improve_tile raises — must surface as a string
            r = bridge.act("improve_tile", {"unit_index": 9,
                                            "improvement_name": "IMPROVE_FARM"})
            self.assertTrue(r.startswith("Error:"), msg=r)
            self.assertIn("tuner exploded", r)
            d = bridge.act_data("improve_tile", {"unit_index": 9,
                                                 "improvement_name": "IMPROVE_FARM"})
            self.assertIn("error", d)
        finally:
            asyncio.run(bridge.stop())

    def test_act_before_start_is_an_error_string(self):
        bridge, _ = _make_bridge()
        r = bridge.act("end_turn", {})
        self.assertTrue(r.startswith("Error:"), msg=r)

    def test_reconnect_on_lost_link(self):
        bridge, holder = _make_bridge()
        asyncio.run(bridge.start())
        try:
            old_conn = holder["conn"]
            old_conn.connected = False  # link drops
            r = bridge.act("end_turn", {"fast": True})
            self.assertEqual(r, "Turn 5 -> 6 | fast end_turn — narration/autosave skipped")
            new_conn = holder["conn"]
            self.assertIsNot(new_conn, old_conn)
            self.assertEqual(new_conn.connect_calls, 1)
            self.assertEqual(holder["gs"].calls, [("end_turn", {"fast": True})])
        finally:
            asyncio.run(bridge.stop())


class ReadTests(unittest.TestCase):
    def test_units_and_turnstate(self):
        bridge, holder = _make_bridge()
        asyncio.run(bridge.start())
        try:
            self.assertEqual(bridge.units(),
                             [{"unit_index": 1, "unit_type": "UNIT_SETTLER"}])
            self.assertEqual(bridge.turnstate(), {"turn": 5})
            self.assertEqual([n for n, _ in holder["gs"].calls],
                             ["get_units", "get_quick_state"])
        finally:
            asyncio.run(bridge.stop())

    def test_production_options_via_action_dispatch(self):
        bridge, holder = _make_bridge()
        asyncio.run(bridge.start())
        try:
            opts = bridge.production_options(42)
            self.assertEqual(opts, [{"item_name": "UNIT_WARRIOR",
                                     "category": "UNIT", "turns": 3}])
            self.assertEqual(holder["gs"].calls,
                             [("list_city_production", {"city_id": 42})])
        finally:
            asyncio.run(bridge.stop())


class WarroomCollectTests(unittest.TestCase):
    def _patched_parsers(self):
        import civ_mcp.lua as lq

        def _mk(name):
            m = mock.MagicMock()
            if name == "parse_cities_response":
                m.side_effect = lambda lines: ([{"city": "c"}], ["dist"])
            else:
                m.side_effect = lambda lines, _n=name: {"parser": _n,
                                                        "lines": list(lines)}
            return m

        names = ("parse_wr_lines", "parse_overview_response",
                 "parse_units_response", "parse_cities_response",
                 "parse_tech_civics_response", "parse_threat_scan_response",
                 "parse_policies_response", "parse_city_states_response",
                 "parse_governors_response")
        mods = {n: _mk(n) for n in names}
        return lq, names, mods

    def test_sections_flags_and_shapes(self):
        lq, _names, mods = self._patched_parsers()
        bridge, holder = _make_bridge()
        asyncio.run(bridge.start())
        try:
            conn = holder["conn"]
            with mock.patch.multiple(lq, **mods):
                # base collect: no low-frequency sections requested
                col = bridge.warroom_collect()
                self.assertNotIn("SECTION|policies", conn.write_luas[0])
                self.assertNotIn("SECTION|cs", conn.write_luas[0])
                mods["parse_policies_response"].assert_not_called()
                mods["parse_city_states_response"].assert_not_called()
                mods["parse_governors_response"].assert_not_called()
                self.assertIsNone(col["policies"])
                self.assertIsNone(col["city_states"])
                self.assertIsNone(col["governors"])
                # every section routed to its parser with the right lines
                self.assertEqual(col["overview"],
                                 {"parser": "parse_overview_response",
                                  "lines": ["OVL"]})
                self.assertEqual(col["units"],
                                 {"parser": "parse_units_response",
                                  "lines": ["UL"]})
                self.assertEqual(col["cities"], [[{"city": "c"}], ["dist"]])
                self.assertEqual(col["tech"],
                                 {"parser": "parse_tech_civics_response",
                                  "lines": ["TL"]})
                self.assertEqual(col["threats"],
                                 {"parser": "parse_threat_scan_response",
                                  "lines": ["THL"]})
                self.assertEqual(col["wr"],
                                 {"parser": "parse_wr_lines",
                                  "lines": ["WRLINE"]})
                # tech/threats ride the GameCore read, the rest the write
                self.assertIn("SECTION|tech", conn.read_luas[0])
                self.assertIn("SECTION|overview", conn.write_luas[0])

                # flagged collect: sections appended, parsers engaged
                for m in mods.values():
                    m.reset_mock()
                conn.write_lines = conn.write_lines + [
                    "SECTION|policies", "PL",
                    "SECTION|cs", "CSL",
                    "SECTION|gov", "GOVL",
                ]
                col2 = bridge.warroom_collect(pol=True, cs=True, gov=True)
                self.assertIn("SECTION|policies", conn.write_luas[1])
                self.assertIn("SECTION|cs", conn.write_luas[1])
                self.assertIn("SECTION|gov", conn.write_luas[1])
                mods["parse_policies_response"].assert_called_once_with(["PL"])
                mods["parse_city_states_response"].assert_called_once_with(["CSL"])
                mods["parse_governors_response"].assert_called_once_with(["GOVL"])
                self.assertEqual(col2["policies"],
                                 {"parser": "parse_policies_response",
                                  "lines": ["PL"]})
                self.assertEqual(col2["city_states"],
                                 {"parser": "parse_city_states_response",
                                  "lines": ["CSL"]})
                self.assertEqual(col2["governors"],
                                 {"parser": "parse_governors_response",
                                  "lines": ["GOVL"]})
        finally:
            asyncio.run(bridge.stop())

    def test_collect_error_surfaces_as_error_dict(self):
        bridge, holder = _make_bridge()
        asyncio.run(bridge.start())
        try:
            async def _boom(lua_code, timeout=5.0):
                raise ConnectionError("link died mid-collect")
            holder["conn"].execute_write = _boom
            col = bridge.warroom_collect()
            self.assertIn("error", col)
        finally:
            asyncio.run(bridge.stop())


class ContractTests(unittest.TestCase):
    def test_action_tools_match_web_api(self):
        """The whitelist is the compat contract — must equal web_api's."""
        from civ_mcp.web_api import ACTION_TOOLS as web_tools
        self.assertEqual(ACTION_TOOLS, web_tools)

    def test_import_needs_no_game(self):
        import importlib
        import server.bridge_inproc as mod
        fresh = importlib.reload(mod)  # noqa: F841 — must not touch the game
        self.assertTrue(callable(fresh.InProcessBridge))


if __name__ == "__main__":
    unittest.main()
