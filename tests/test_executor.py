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


def _make_pilot():
    return AutoPilot(FakeJournal(), evaluate,
                     lambda *a, **k: ({}, 0), cfg=Config())


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

    async def test_threat_attacks_when_targets_present(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"attack_unit": "ATTACKING|52,12"})
        snapshot = {
            "turn": 5,
            "threats": [{"type": "UNIT_ARCHER", "at": [52, 12], "distance": 1}],
            "units": [{"unit_index": 1, "type": "UNIT_WARRIOR", "at": [52, 13],
                       "cs": 20, "moves": 2,
                       "targets": ["UNIT_ARCHER@52,12(70hp)"]}],
        }
        await pilot._execute(
            bridge, {"threat_response": {"type": "noul", "noul": 0.7}},
            {}, snapshot, 5)
        self.assertEqual(bridge.calls[0][0], "attack_unit")
        self.assertEqual((bridge.calls[0][1]["target_x"],
                          bridge.calls[0][1]["target_y"]), (52, 12))

    async def test_threat_move_dedup_within_turn(self):
        pilot = _make_pilot()
        bridge = FakeBridge({"move_unit": "MOVING_TO|50,14"})
        snapshot = {
            "turn": 5,
            "threats": [{"type": "UNIT_WARRIOR", "at": [50, 15], "distance": 2}],
            "units": [{"unit_index": 1, "type": "UNIT_WARRIOR", "at": [50, 13],
                       "cs": 20, "moves": 2, "targets": []}],
        }
        await pilot._respond_threat(bridge, snapshot, 5)
        n_first = len(bridge.calls)
        await pilot._respond_threat(bridge, snapshot, 5)  # same turn again
        self.assertEqual(len(bridge.calls), n_first)      # deduped
        pilot._tried_moves = set()
        await pilot._respond_threat(bridge, snapshot, 6)  # new turn → allowed
        self.assertGreater(len(bridge.calls), n_first)


if __name__ == "__main__":
    unittest.main()
