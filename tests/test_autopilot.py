# -*- coding: utf-8 -*-
"""Autopilot helper tests: end_turn classification, turn parsing, snapshots."""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.autopilot import (  # noqa: E402
    _hex_distance,
    _settle_desc,
    build_judge_state,
    build_snapshot,
    classify_end_turn,
    parse_turn_advance,
)

REAL_WC = ("World Congress fires this turn (2 resolution(s), 0 favor). "
           "Use get_world_congress() to review resolutions and targets, then "
           "queue_wc_votes() to register your votes, then call end_turn() again.")


class ClassifyTests(unittest.TestCase):
    def test_real_world_congress_string(self):
        self.assertEqual(classify_end_turn(REAL_WC), "world_congress")

    def test_success_narration(self):
        t = ("Turn 15 -> 16 | Score: 14\n\n== Events ==\n"
             "  -- SLOW GROWTH: Pokrovka (17t to next pop, +1.0/t)")
        self.assertEqual(classify_end_turn(t), "ok")
        self.assertEqual(parse_turn_advance(t, 15), 16)
        self.assertIsNone(parse_turn_advance(t, 16))

    def test_timeout_and_hang(self):
        self.assertEqual(classify_end_turn("Error: timed out"), "timeout")
        self.assertEqual(
            classify_end_turn("HANG:121:save|End turn requested (turn is still 121)."),
            "hang")

    def test_game_over(self):
        self.assertEqual(
            classify_end_turn("GAME OVER - DEFEAT. Trajan of Rome won a "
                              "Science victory. The game has ended."),
            "game_over")

    def test_blockers(self):
        m = ("Cannot end turn — resolve these blockers:\n"
             "  - Production (empty queue in X)  ->  Set production ...")
        self.assertEqual(classify_end_turn(m), "blockers")

    def test_deal_and_diplomacy(self):
        self.assertEqual(
            classify_end_turn("Turn paused — incoming trade deal:\n  ..."),
            "trade_deal")
        self.assertEqual(
            classify_end_turn("Cannot end turn: diplomacy encounter pending with "
                              "Trajan. Use respond_to_diplomacy to handle it."),
            "diplomacy")


class HelperTests(unittest.TestCase):
    def test_hex_distance(self):
        self.assertEqual(_hex_distance(0, 0, 0, 0), 0)
        self.assertEqual(_hex_distance(0, 0, 1, 0), 1)
        self.assertEqual(_hex_distance(0, 0, 0, 2), 2)

    def test_settle_desc(self):
        d = _settle_desc({"score": 9.5, "total_food": 4, "total_prod": 3,
                          "water_type": "fresh", "resources": ["S:IRON"],
                          "defense_score": 2})
        self.assertIn("9.5", d)
        self.assertIn("IRON", d)


class SnapshotTests(unittest.TestCase):
    def _ov(self):
        return {"turn": 30, "civ_name": "Scythia", "leader_name": "Tomyris",
                "difficulty": "Prince", "score": 20, "gold": 50,
                "gold_per_turn": 3, "science_yield": 4, "culture_yield": 4,
                "faith": 0}

    def _tech(self):
        return {
            "current_research": "Pottery", "current_research_turns": 5,
            "current_civic": "Code of Laws", "current_civic_turns": 2,
            "available_techs": [{"name": "Mining",
                                 "tech_type": "TECHNOLOGY_MINING",
                                 "turns": 8, "unlocks": "Mine"}],
            "available_civics": [{"name": "Craftsmanship",
                                  "civic_type": "CIVICS_CRAFTSMANSHIP",
                                  "turns": 6}],
        }

    def test_build_snapshot_and_judge_state(self):
        units = [{"unit_index": 1, "unit_type": "UNIT_WARRIOR", "x": 50, "y": 13,
                  "moves_remaining": 2.0, "health": 100, "max_health": 100,
                  "combat_strength": 20, "targets": []}]
        cities = [{"city_id": 1, "name": "Pokrovka", "x": 50, "y": 13,
                   "population": 2, "turns_to_grow": 5,
                   "currently_building": "NONE", "food_surplus": 1.0,
                   "defense_strength": 10, "garrison_unit": "",
                   "districts": [], "unimproved_resources": []}]
        threats = [{"unit_type": "UNIT_ARCHER", "x": 52, "y": 12,
                    "combat_strength": 15, "hp": 100, "distance": 1,
                    "owner_name": "Barbarian"}]
        snap = build_snapshot(
            self._ov(), units, cities, self._tech(), threats,
            prod_by_city=[{"city_id": 1, "name": "Pokrovka",
                           "options": [{"category": "UNIT",
                                        "item_name": "UNIT_WARRIOR",
                                        "turns": 3}]}],
            settle_candidates=[{"x": 44, "y": 12, "score": 9.0, "desc": "x"}],
            map_tiles=[])
        self.assertEqual(snap["turn"], 30)
        self.assertEqual(snap["cities"][0]["production"], "")  # NONE → idle
        self.assertEqual(snap["available"]["techs"][0]["id"], "TECH_MINING")
        self.assertEqual(snap["available"]["civics"][0]["id"],
                         "CIVIC_CRAFTSMANSHIP")
        self.assertEqual(snap["available"]["production_by_city"][0]["options"][0]["id"],
                         "UNIT_WARRIOR")
        js = build_judge_state(snap)
        for k in ("situation", "empire", "threats", "settle_candidates", "available"):
            self.assertIn(k, js)
        self.assertEqual(js["empire"]["units"][0]["hp"], "100/100")


if __name__ == "__main__":
    unittest.main()
