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
        # a decided strategy keeps the baseline snapshot question-free
        "strategy": {"path": "science", "since_turn": 30},
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
        tactics = [q for q in r["questions"] if q.startswith("tactics:")]
        self.assertTrue(tactics)
        q = r["questions"][tactics[0]]
        self.assertIn("new_threat", r["triggers"])
        self.assertIn("advance", q["criteria"])
        self.assertIn("fortify", q["criteria"])
        r2 = evaluate(s, s)  # same threats, no attack targets → not fresh
        self.assertFalse([q for q in r2["questions"] if q.startswith("tactics:")])

    def test_attack_available_without_fresh_threat(self):
        s = _snap(units=[{"unit_index": 1, "type": "UNIT_WARRIOR", "at": [50, 13],
                          "cs": 20, "hp": 100, "max_hp": 100, "moves": 2,
                          "targets": ["UNIT_SCOUT@50,14(30hp)"]}],
                  threats=[{"type": "UNIT_SCOUT", "at": [50, 14], "cs": 10,
                            "hp": 30, "distance": 1, "owner": "Barbarian"}])
        r = evaluate(s, s)  # identical snapshots → nothing "fresh"
        self.assertIn("attack_available", r["triggers"])
        q = r["questions"].get("tactics:1")
        self.assertIsNotNone(q)
        self.assertIn("attack:50,14", q["criteria"])
        self.assertIn("engine-verified", q["criteria"]["attack:50,14"])

    def test_wounded_unit_offered_retreat(self):
        s = _snap(threats=[{"type": "UNIT_ARCHER", "at": [52, 12], "cs": 15,
                            "hp": 100, "distance": 1, "owner": "Barbarian"}],
                  units=[{"unit_index": 1, "type": "UNIT_WARRIOR", "at": [50, 13],
                          "cs": 20, "hp": 40, "max_hp": 100, "moves": 2,
                          "targets": []}])
        r = evaluate(s, _snap())
        q = r["questions"]["tactics:1"]
        self.assertIn("retreat", q["criteria"])

    def test_settler_never_gets_tactics_question(self):
        s = _snap(threats=[{"type": "UNIT_ARCHER", "at": [52, 12], "cs": 15,
                            "hp": 100, "distance": 1, "owner": "Barbarian"}],
                  units=[{"unit_index": 0, "type": "UNIT_SETTLER", "at": [50, 13],
                          "cs": 0, "hp": 100, "max_hp": 100, "moves": 2,
                          "targets": []}])
        r = evaluate(s, _snap())
        self.assertFalse([q for q in r["questions"] if q.startswith("tactics:")])

    def test_strategy_asked_when_missing(self):
        r = evaluate(_snap(strategy=None), _snap())
        self.assertIn("strategy_pick", r["questions"])
        self.assertIn("strategy_review", r["triggers"])
        self.assertIn("science", r["questions"]["strategy_pick"]["criteria"])

    def test_strategy_hint_injected_and_reask_after_30t(self):
        s = _snap(strategy={"path": "science", "since_turn": 30},
                  research={"name": "None", "turns_left": -1})
        r = evaluate(s, _snap())
        self.assertNotIn("strategy_pick", r["questions"])   # fresh enough
        self.assertIn("science victory",
                      r["questions"]["research_pick"]["instructions"])
        s2 = _snap(strategy={"path": "science", "since_turn": 0},
                   research={"name": "None", "turns_left": -1})
        r2 = evaluate(s2, _snap())
        self.assertIn("strategy_pick", r2["questions"])     # 30 turns old

    def test_policy_review_asks_when_due(self):
        s = _snap(policies={
            "government": "Chiefdom", "review_age": 20,
            "slots": [{"slot_index": 0, "slot_type": "SLOT_ECONOMIC",
                       "current": "POLICY_GOD_KING"}],
            "options_by_slot": {"0": {
                "POLICY_GOD_KING": "God King — gold/faith (current)",
                "POLICY_URBAN_PLANNING": "Urban Planning — +1 production"}},
        })
        r = evaluate(s, _snap())
        q = r["questions"].get("policy_pick:0")
        self.assertIsNotNone(q)
        self.assertIn("policy_review", r["triggers"])
        self.assertIn("POLICY_URBAN_PLANNING", q["criteria"])

    def test_policy_no_review_when_fresh(self):
        s = _snap(policies={
            "government": "Chiefdom", "review_age": 3,
            "slots": [{"slot_index": 0, "slot_type": "SLOT_ECONOMIC",
                       "current": "POLICY_GOD_KING"}],
            "options_by_slot": {"0": {"POLICY_GOD_KING": "x",
                                      "POLICY_URBAN_PLANNING": "y"}},
        })
        r = evaluate(s, _snap())
        self.assertNotIn("policy_pick:0", r["questions"])

    def test_envoy_pick_when_tokens_available(self):
        s = _snap(city_states=[
            {"player_id": 5, "name": "Geneva", "city_state_type": "Religious",
             "envoys_sent": 1, "suzerain_name": "None", "can_send_envoy": True}])
        r = evaluate(s, _snap())
        q = r["questions"].get("envoy_pick")
        self.assertIsNotNone(q)
        self.assertIn("5", q["criteria"])
        self.assertIn("Geneva", q["criteria"]["5"])

    def test_no_envoy_question_without_city_states(self):
        r = evaluate(_snap(), _snap())
        self.assertNotIn("envoy_pick", r["questions"])

    def test_production_gets_key_item_and_army_notes(self):
        s = _snap(
            strategy={"path": "religion", "since_turn": 100},
            cities=[{"city_id": 7, "name": "Tbilisi", "pop": 8,
                     "growth": "5t", "production": "", "at": [64, 24]}],
            available={"production_by_city": [
                {"city_id": 7, "name": "Tbilisi", "options": [
                    {"id": "UNIT_WARRIOR", "desc": "UNIT · 3t"},
                    {"id": "DISTRICT_HOLY_SITE", "desc": "DISTRICT · 10t"}]}]},
            units=[{"unit_index": i, "type": "UNIT_WARRIOR", "cs": 20,
                    "moves": 0, "targets": []} for i in range(8)],
            threats=[])
        r = evaluate(s, _snap())
        ins = r["questions"]["production_pick"]["instructions"]
        self.assertIn("CRITICAL", ins)                 # Holy Site flagged
        self.assertIn("Our military: 8 units", ins)    # army context
        self.assertIn("prefer economy", ins)           # stand-down nudge

    def test_research_gets_key_item_note(self):
        s = _snap(strategy={"path": "religion", "since_turn": 100},
                  research={"name": "None", "turns_left": -1},
                  available={"techs": [{"id": "TECH_ASTROLOGY", "desc": "13t"}]})
        r = evaluate(s, _snap())
        self.assertIn("CRITICAL", r["questions"]["research_pick"]["instructions"])

    def test_no_key_note_without_strategy(self):
        s = _snap(strategy=None,
                  research={"name": "None", "turns_left": -1},
                  available={"techs": [{"id": "TECH_ASTROLOGY", "desc": "13t"}]})
        r = evaluate(s, _snap())
        self.assertNotIn("CRITICAL",
                         r["questions"]["research_pick"]["instructions"])

    def test_strategy_reasked_in_new_campaign(self):
        s = _snap(strategy={"path": "religion", "since_turn": 97})
        s["turn"] = 2  # new game, old strategy stamps from T97
        r = evaluate(s, _snap())
        self.assertIn("strategy_pick", r["questions"])

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
