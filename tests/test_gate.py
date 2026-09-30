# -*- coding: utf-8 -*-
"""Decision-gate unit tests (stdlib unittest — run from project root):

    python -m unittest discover -s tests -v
"""
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.decision_gate import evaluate  # noqa: E402


def _snap(**over):
    s = {
        "turn": 30, "civ": "Scythia", "leader": "Tomyris", "difficulty": "Prince",
        "score": 20, "yields": {"gold": 50, "gold_per_turn": 3, "science": 4,
                                "culture": 4, "faith": 0},
        "research": {"name": "Pottery", "turns_left": 5},
        "civic": {"name": "Code of Laws", "turns_left": 2},
        "cities": [{"city_id": 1, "name": "Pokrovka", "pop": 2, "growth": "5t",
                    "production": "UNIT_WARRIOR", "at": [50, 13]}],
        "units": [{"unit_index": 0, "type": "UNIT_WARRIOR", "at": [50, 13],
                   "cs": 20, "hp": 100, "max_hp": 100, "moves": 2, "targets": []}],
        "threats": [],
        "notes": [],
        "available": {
            "techs": [{"id": "TECH_MINING", "desc": "8t"}],
            "civics": [{"id": "CIVIC_CRAFTSMANSHIP", "desc": "6t"}],
            "production_by_city": [
                {"city_id": 1, "name": "Pokrovka",
                 "options": [{"id": "UNIT_WARRIOR", "desc": "UNIT · 3t"}]}],
        },
        "settle_candidates": [],
        "map_near_capital": [],
        "idle_settler_count": 0,
    }
    s.update(over)
    return s


class GateTests(unittest.TestCase):
    def test_no_decision_point_skips(self):
        r = evaluate(_snap(), _snap())
        self.assertFalse(r["should_ask"])
        self.assertTrue(r["skip_reason"])
        self.assertEqual(r["questions"], {})

    def test_research_idle_asks(self):
        s = _snap(research={"name": "None", "turns_left": -1})
        r = evaluate(s, _snap())
        self.assertTrue(r["should_ask"])
        q = r["questions"]["research_pick"]
        self.assertIn("TECH_MINING", q["criteria"])

    def test_civic_idle_asks(self):
        s = _snap(civic=None)
        r = evaluate(s, _snap())
        self.assertIn("civic_pick", r["questions"])

    def test_production_idle_per_city(self):
        s = _snap(
            cities=[{"city_id": 7, "name": "Pokrovka", "pop": 2,
                     "growth": "5t", "production": "", "at": [50, 13]}],
            available={"production_by_city": [
                {"city_id": 7, "name": "Pokrovka",
                 "options": [{"id": "UNIT_WARRIOR", "desc": "UNIT · 3t"}]}]},
        )
        r = evaluate(s, _snap())
        q = r["questions"]["production_pick"]
        self.assertEqual(q["city_id"], 7)
        self.assertIn("UNIT_WARRIOR", q["criteria"])

    def test_new_threat_diff(self):
        s = _snap(threats=[{"type": "UNIT_ARCHER", "at": [52, 12], "cs": 15,
                            "hp": 100, "distance": 1, "owner": "Barbarian"}])
        r = evaluate(s, _snap())
        self.assertIn("threat_response", r["questions"])
        r2 = evaluate(s, s)  # same threats → not fresh
        self.assertNotIn("threat_response", r2["questions"])

    def test_settle_candidates(self):
        s = _snap(settle_candidates=[{"x": 44, "y": 12, "score": 9.5, "desc": "hi"}])
        r = evaluate(s, _snap())
        q = r["questions"]["settle_pick"]
        self.assertEqual(q["criteria"]["44,12"], "hi")

    def test_state_keys_referenced_exist_in_judge_state(self):
        """Question instructions must only reference keys build_judge_state
        actually provides — the bug that broke the first campaign."""
        import re
        from server.autopilot import build_judge_state
        s = _snap(research={"name": "None", "turns_left": -1}, civic=None,
                  threats=[{"type": "UNIT_ARCHER", "at": [52, 12], "cs": 15,
                            "hp": 100, "distance": 1, "owner": "Barbarian"}],
                  settle_candidates=[{"x": 44, "y": 12, "score": 9.5, "desc": "hi"}],
                  cities=[{"city_id": 7, "name": "Pokrovka", "pop": 2,
                           "growth": "5t", "production": "", "at": [50, 13]}])
        r = evaluate(s, _snap())
        js = build_judge_state(s)
        referenced = set()
        for q in r["questions"].values():
            referenced |= set(re.findall(r"state\.(\w+)",
                                         q.get("instructions", "")))
        self.assertTrue(referenced)
        for key in referenced:
            self.assertIn(key, js, f"judge state missing referenced key: {key}")


if __name__ == "__main__":
    unittest.main()
