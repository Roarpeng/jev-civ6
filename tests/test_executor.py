# -*- coding: utf-8 -*-
"""Executor wiring tests with a fake bridge — verifies the actions the
autopilot would send for each question type, entirely offline."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.autopilot import AutoPilot  # noqa: E402
from server.config import Config  # noqa: E402
from server.decision_gate import evaluate  # noqa: E402


class FakeJournal:
    def __init__(self):
        self.events = []

    def add(self, type, data, turn=None):
        self.events.append((type, data, turn))
        return {"id": len(self.events), "ts": "t"}


class FakeBridge:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def act(self, tool, args, timeout=60.0):
        self.calls.append((tool, dict(args)))
        r = self.results.get(tool)
        if callable(r):
            return r(tool, args)
        return "OK" if r is None else r

    def act_data(self, tool, args, timeout=60.0):
        self.calls.append((tool, dict(args)))
        r = self.results.get(tool)
        if callable(r):
            return r(tool, args)
        if isinstance(r, list):
            return r
        return [{"unit_index": None, "result": "OK"}]


def _make_pilot():
    pilot = AutoPilot(FakeJournal(), evaluate,
                     lambda *a, **k: ({}, 0), cfg=Config())
    pilot._save_state = lambda: None  # never touch the real state file
    return pilot


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_research_and_civic_executed(self):
        pilot = _make_pilot()
        bridge = FakeBridge()
        snapshot = {"turn": 5, "units": [], "cities": [], "threats": []}
        answers = {"research_pick": {"type": "choice", "choice": "TECH_MINING"},
                   "civic_pick": {"type": "choice",
                                  "choice": "CIVIC_CRAFTSMANSHIP"}}
        await pilot._execute(bridge, answers, {}, snapshot, 5)
        self.assertEqual([c[0] for c in bridge.calls], ["set_research", "set_civic"])
        self.assertEqual(bridge.calls[0][1]["tech_name"], "TECH_MINING")
        self.assertEqual(bridge.calls[1][1]["civic_name"], "CIVIC_CRAFTSMANSHIP")

    async def test_production_uses_city_meta(self):
        pilot = _make_pilot()
        bridge = FakeBridge()
        snapshot = {"turn": 5, "units": [], "cities": [], "threats": []}
        answers = {"production_pick": {"type": "choice", "choice": "UNIT_WARRIOR"}}
        questions = {"production_pick": {"city_id": 7,
                                         "criteria": {"UNIT_WARRIOR": ""}}}
        await pilot._execute(bridge, answers, questions, snapshot, 5)
        self.assertEqual(bridge.calls[0][0], "set_city_production")
        self.assertEqual(bridge.calls[0][1]["city_id"], 7)
        self.assertEqual(bridge.calls[0][1]["item_type"], "UNIT")
        self.assertEqual(bridge.calls[0][1]["item_name"], "UNIT_WARRIOR")

    async def test_settler_founds_when_on_target(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"found_city": "FOUNDED|10,10"})
        snapshot = {"turn": 5, "threats": [],
                    "units": [{"unit_index": 3, "type": "UNIT_SETTLER",
                               "at": [10, 10], "moves": 2, "cs": 0}]}
        await pilot._execute(
            bridge, {"settle_pick": {"type": "choice", "choice": "10,10"}},
            {}, snapshot, 5)
        self.assertIn("found_city", [c[0] for c in bridge.calls])
        self.assertIsNone(pilot._settler_plan)

    async def test_settler_moves_toward_target(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"move_unit": "MOVING_TO|9,10"})
        snapshot = {"turn": 5, "threats": [],
                    "units": [{"unit_index": 3, "type": "UNIT_SETTLER",
                               "at": [8, 10], "moves": 2, "cs": 0}]}
        await pilot._execute(
            bridge, {"settle_pick": {"type": "choice", "choice": "10,10"}},
            {}, snapshot, 5)
        self.assertEqual(bridge.calls[0][0], "move_unit")
        self.assertEqual(bridge.calls[0][1]["target_x"], 10)
        self.assertEqual(bridge.calls[0][1]["target_y"], 10)

    async def test_tactics_attack_executes_verified_target(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"attack_unit": "ATTACKING|52,12"})
        snapshot = {
            "turn": 5,
            "threats": [{"type": "UNIT_ARCHER", "at": [52, 12], "distance": 1}],
            "units": [{"unit_index": 1, "type": "UNIT_WARRIOR", "at": [52, 13],
                       "cs": 20, "moves": 2,
                       "targets": ["UNIT_ARCHER@52,12(70hp)"]}],
        }
        answers = {"tactics:1": {"type": "choice", "choice": "attack:52,12"}}
        await pilot._execute(bridge, answers, {"tactics:1": {"unit_index": 1}},
                             snapshot, 5)
        self.assertEqual(bridge.calls[0][0], "attack_unit")
        self.assertEqual((bridge.calls[0][1]["target_x"],
                          bridge.calls[0][1]["target_y"]), (52, 12))
        self.assertEqual(bridge.calls[0][1]["unit_index"], 1)

    async def test_tactics_attack_error_falls_back_to_fortify(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"attack_unit": "Error: not attackable"})
        snapshot = {"turn": 5, "threats": [], "units": [
            {"unit_index": 1, "type": "UNIT_WARRIOR", "at": [52, 13],
             "cs": 20, "moves": 2, "targets": []}]}
        answers = {"tactics:1": {"type": "choice", "choice": "attack:52,12"}}
        await pilot._execute(bridge, answers, {"tactics:1": {"unit_index": 1}},
                             snapshot, 5)
        self.assertEqual([c[0] for c in bridge.calls],
                         ["attack_unit", "fortify_unit"])

    async def test_tactics_retreat_moves_toward_city(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"move_units_batch": [
            {"unit_index": 1, "result": "MOVING_TO|50,13|from:52,13|now_at:51,13|(moved dx:-1)"}]})
        snapshot = {"turn": 5, "threats": [], "cities": [
            {"city_id": 1, "name": "Pokrovka", "at": [50, 13]}], "units": [
            {"unit_index": 1, "type": "UNIT_WARRIOR", "at": [52, 13],
             "cs": 20, "moves": 2, "hp": 40, "targets": []}]}
        answers = {"tactics:1": {"type": "choice", "choice": "retreat"}}
        await pilot._execute(bridge, answers, {"tactics:1": {"unit_index": 1}},
                             snapshot, 5)
        batch = [c for c in bridge.calls if c[0] == "move_units_batch"]
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0][1]["moves"][0]["target_x"], 50)
        self.assertEqual(batch[0][1]["moves"][0]["target_y"], 13)

    async def test_tactics_fortify_choice_batches(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"fortify_units": [
            {"unit_index": 1, "result": "FORTIFIED"}]})
        snapshot = {"turn": 5, "threats": [], "units": [
            {"unit_index": 1, "type": "UNIT_WARRIOR", "at": [52, 13],
             "cs": 20, "moves": 2, "targets": []}]}
        answers = {"tactics:1": {"type": "choice", "choice": "fortify"}}
        await pilot._execute(bridge, answers, {"tactics:1": {"unit_index": 1}},
                             snapshot, 5)
        fort = [c for c in bridge.calls if c[0] == "fortify_units"]
        self.assertEqual(len(fort), 1)
        self.assertEqual(fort[0][1]["unit_indexes"], [1])

    async def test_tactics_all_marches_in_one_batch(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"move_units_batch": [
            {"unit_index": 1, "result": "MOVING_TO|50,15|from:50,13"},
            {"unit_index": 2, "result": "MOVING_TO|50,15|from:50,14"}]})
        snapshot = {
            "turn": 5,
            "threats": [{"type": "UNIT_WARRIOR", "at": [50, 15], "distance": 2}],
            "units": [
                {"unit_index": 1, "type": "UNIT_WARRIOR", "at": [50, 13],
                 "cs": 20, "moves": 2, "targets": []},
                {"unit_index": 2, "type": "UNIT_WARRIOR", "at": [50, 14],
                 "cs": 20, "moves": 2, "targets": []}],
        }
        answers = {"tactics:1": {"type": "choice", "choice": "advance"},
                   "tactics:2": {"type": "choice", "choice": "advance"}}
        await pilot._execute(bridge, answers, {}, snapshot, 5)
        batch = [c for c in bridge.calls if c[0] == "move_units_batch"]
        self.assertEqual(len(batch), 1)  # both marches, ONE call
        self.assertEqual(len(batch[0][1]["moves"]), 2)

    async def test_tactics_advance_move_dedup_within_turn(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"move_units_batch": [
            {"unit_index": 1, "result": "MOVING_TO|50,15|from:50,13"}]})
        snapshot = {
            "turn": 5,
            "threats": [{"type": "UNIT_WARRIOR", "at": [50, 15], "distance": 2}],
            "units": [{"unit_index": 1, "type": "UNIT_WARRIOR", "at": [50, 13],
                       "cs": 20, "moves": 2, "targets": []}],
        }
        answers = {"tactics:1": {"type": "choice", "choice": "advance"}}
        await pilot._execute(bridge, answers, {}, snapshot, 5)
        n_first = len([c for c in bridge.calls if c[0] == "move_units_batch"])
        await pilot._execute(bridge, answers, {}, snapshot, 5)  # same turn again
        self.assertEqual(
            len([c for c in bridge.calls if c[0] == "move_units_batch"]),
            n_first)  # deduped
        pilot._tried_moves = set()
        await pilot._execute(bridge, answers, {}, snapshot, 6)  # new turn
        self.assertGreater(
            len([c for c in bridge.calls if c[0] == "move_units_batch"]),
            n_first)

    async def test_unasked_military_units_fortify_by_default(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"fortify_units": [
            {"unit_index": 1, "result": "FORTIFIED"},
            {"unit_index": 2, "result": "FORTIFIED"}]})
        snapshot = {"turn": 5, "threats": [
            {"type": "UNIT_SCOUT", "at": [60, 20], "distance": 3}], "cities": [], "units": [
            {"unit_index": 1, "type": "UNIT_WARRIOR", "at": [50, 13],
             "cs": 20, "moves": 2, "targets": []},
            {"unit_index": 2, "type": "UNIT_WARRIOR", "at": [51, 13],
             "cs": 20, "moves": 2, "targets": []}]}
        answers = {"tactics:1": {"type": "choice", "choice": "fortify"}}
        await pilot._execute(bridge, answers, {"tactics:1": {"unit_index": 1}},
                             snapshot, 5)
        fort = [c for c in bridge.calls if c[0] == "fortify_units"]
        self.assertEqual(len(fort), 1)
        # asked unit 1 fortified by its own choice; unasked unit 2 included
        self.assertEqual(fort[0][1]["unit_indexes"], [1, 2])

    async def test_batch_move_error_falls_back_to_fortify(self):
        pilot = _make_pilot()
        bridge = FakeBridge({
            "move_units_batch": [
                {"unit_index": 1, "result": "Error: NO_MOVES|out of moves"}],
            "fortify_unit": "FORTIFIED"})
        snapshot = {
            "turn": 5,
            "threats": [{"type": "UNIT_WARRIOR", "at": [50, 15], "distance": 2}],
            "units": [{"unit_index": 1, "type": "UNIT_WARRIOR", "at": [50, 13],
                       "cs": 20, "moves": 2, "targets": []}],
        }
        answers = {"tactics:1": {"type": "choice", "choice": "advance"}}
        await pilot._execute(bridge, answers, {}, snapshot, 5)
        self.assertIn("fortify_unit", [c[0] for c in bridge.calls])

    async def test_strategy_pick_stored_and_journaled(self):
        pilot = _make_pilot()
        bridge = FakeBridge()
        snapshot = {"turn": 40, "units": [], "cities": [], "threats": []}
        await pilot._execute(
            bridge, {"strategy_pick": {"type": "choice", "choice": "science"}},
            {}, snapshot, 40)
        self.assertEqual(pilot._strategy, {"path": "science", "since_turn": 40})
        self.assertIn("strategy", [e[1]["tool"] for e in pilot.journal.events
                                   if isinstance(e[1], dict) and "tool" in e[1]])

    async def test_policy_pick_sends_assignments(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"set_policies": "POLICIES_SET"})
        snapshot = {"turn": 40, "units": [], "cities": [], "threats": []}
        await pilot._execute(
            bridge,
            {"policy_pick:0": {"type": "choice",
                               "choice": "POLICY_URBAN_PLANNING"}},
            {}, snapshot, 40)
        call = [c for c in bridge.calls if c[0] == "set_policies"]
        self.assertEqual(call[0][1]["assignments"],
                         {"0": "POLICY_URBAN_PLANNING"})

    async def test_envoy_pick_sends_envoy(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"send_envoy": "ENVOY_SENT|Geneva"})
        snapshot = {"turn": 50, "units": [], "cities": [], "threats": []}
        await pilot._execute(
            bridge, {"envoy_pick": {"type": "choice", "choice": "5"}},
            {}, snapshot, 50)
        call = [c for c in bridge.calls if c[0] == "send_envoy"]
        self.assertEqual(call[0][1]["city_state_player_id"], 5)

    async def test_builder_improves_current_tile(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"improve_tile": "IMPROVED|MINE"})
        snapshot = {"turn": 9, "cities": [], "threats": [], "units": [
            {"unit_index": 4, "type": "UNIT_BUILDER", "at": [50, 13],
             "cs": 0, "moves": 2, "valid_improvements": ["IMPROVEMENT_MINE"]}]}
        await pilot._advance_builders(bridge, snapshot, 9)
        self.assertEqual(bridge.calls[0][0], "improve_tile")
        self.assertEqual(bridge.calls[0][1]["improvement_name"],
                         "IMPROVEMENT_MINE")

    async def test_builder_walks_to_nearest_unimproved(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"move_unit": "MOVING_TO|52,12"})
        snapshot = {"turn": 9, "threats": [], "units": [
            {"unit_index": 4, "type": "UNIT_BUILDER", "at": [50, 13],
             "cs": 0, "moves": 2, "valid_improvements": []}],
            "cities": [{"city_id": 1, "name": "Pokrovka",
                        "unimproved_resources": ["IRON@52", "12"]}]}
        await pilot._advance_builders(bridge, snapshot, 9)
        self.assertEqual(bridge.calls[0][0], "move_unit")
        self.assertEqual((bridge.calls[0][1]["target_x"],
                          bridge.calls[0][1]["target_y"]), (52, 12))


if __name__ == "__main__":
    unittest.main()
