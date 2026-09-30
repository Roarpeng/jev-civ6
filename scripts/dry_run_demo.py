# -*- coding: utf-8 -*-
"""Offline dry run — no game, no network.

Feeds a realistic late-game snapshot (modeled on the real campaign #1 loss
at T121) through the whole decision pipeline: gate → judge state → LLM
(mock provider) → planned actions; and shows how the end_turn strings that
historically stalled the loop are classified now.

    python scripts/dry_run_demo.py
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.autopilot import build_judge_state, classify_end_turn  # noqa: E402
from server.config import Config, LLMConfig  # noqa: E402
from server.decision_gate import evaluate  # noqa: E402
from server.llm import judge  # noqa: E402

SNAPSHOT = {
    "turn": 120, "civ": "Scythia", "leader": "Tomyris", "difficulty": "Prince",
    "score": 52,
    "yields": {"gold": 886.6, "gold_per_turn": 8.1, "science": 4.1,
               "culture": 4.1, "faith": 0.0},
    "research": {"name": "Iron Working", "turns_left": 9},
    "civic": {"name": "Games and Recreation", "turns_left": 14},
    "cities": [{"city_id": 65536, "name": "Pokrovka", "at": [53, 13], "pop": 5,
                "growth": "114t", "food_surplus": 0.0, "production": "",
                "defense": 20, "garrison": "",
                "districts": ["DISTRICT_CITY_CENTER"],
                "unimproved_resources": ["IVORY", "COTTON", "NITER"]}],
    "units": [
        {"unit_index": 0, "type": "UNIT_SETTLER", "at": [53, 13], "cs": 0,
         "hp": 100, "max_hp": 100, "moves": 2.0, "targets": []},
        {"unit_index": 1, "type": "UNIT_WARRIOR", "at": [53, 13], "cs": 20,
         "hp": 100, "max_hp": 100, "moves": 2.0,
         "targets": ["UNIT_ARCHER@53,12(100hp)"]},
    ],
    "threats": [
        {"type": "UNIT_ARCHER", "at": [53, 12], "cs": 15, "hp": 100,
         "distance": 1, "owner": "Barbarian"},
        {"type": "UNIT_WARRIOR", "at": [55, 13], "cs": 20, "hp": 100,
         "distance": 2, "owner": "Barbarian"},
    ],
    "notes": ["Action required: fill policy slot"],
    "available": {
        "techs": [{"id": "TECH_MACHINERY", "desc": "12t, unlocks: Crossbowman"},
                  {"id": "TECH_STIRRUPS", "desc": "15t, unlocks: Knight"}],
        "civics": [{"id": "CIVIC_DEFENSIVE_TACTICS", "desc": "10t"}],
        "production_by_city": [{
            "city_id": 65536, "name": "Pokrovka",
            "options": [
                {"id": "UNIT_WARRIOR", "desc": "UNIT · 3t"},
                {"id": "UNIT_ARCHER", "desc": "UNIT · 5t"},
                {"id": "BUILDING_GRANARY", "desc": "BUILDING · 5t"},
                {"id": "UNIT_BUILDER", "desc": "UNIT · 4t"},
            ]}],
    },
    "settle_candidates": [
        {"x": 44, "y": 12, "score": 12.5,
         "desc": "score 12.5 · food 5 · prod 3 · fresh water · S:IRON · defense 2"},
    ],
    "map_near_capital": [],
    "idle_settler_count": 1,
}

REAL_WC = ("World Congress fires this turn (2 resolution(s), 0 favor). "
           "Use get_world_congress() to review resolutions and targets, then "
           "queue_wc_votes() to register your votes, then call end_turn() again.")


def main() -> None:
    prev = json.loads(json.dumps(SNAPSHOT))
    prev["threats"] = []

    gate = evaluate(SNAPSHOT, prev)
    print("== GATE ==")
    print(json.dumps({k: gate[k] for k in
                      ("should_ask", "triggers", "question_ids", "skip_reason")},
                     ensure_ascii=False, indent=1))

    judge_state = build_judge_state(SNAPSHOT)
    print("\n== JUDGE STATE (head) ==")
    print(json.dumps(judge_state, ensure_ascii=False, indent=1)[:1400])

    resp, latency = judge(judge_state, gate["questions"],
                          cfg=Config(llm=LLMConfig(provider="mock")))
    print("\n== MOCK JUDGE ANSWERS ==")
    print(json.dumps(resp, ensure_ascii=False, indent=1))

    print("\n== PLANNED EXECUTION (answers → actions) ==")
    for qid, ans in resp["answers"].items():
        if qid == "research_pick":
            print(f"  set_research(tech_name={ans.get('choice')})")
        elif qid == "civic_pick":
            print(f"  set_civic(civic_name={ans.get('choice')})")
        elif qid.startswith("production_pick"):
            print(f"  set_city_production(city_id=65536, item_name={ans.get('choice')})")
        elif qid == "threat_response":
            print(f"  threat_response noul={ans.get('noul')} → attack/move units")
        elif qid == "settle_pick":
            print(f"  plan_settle(target={ans.get('choice')}) → move/found")

    print("\n== END_TURN CLASSIFICATION (the historical stall strings) ==")
    print("  World Congress blocker ->", classify_end_turn(REAL_WC))
    print("  success narration      ->",
          classify_end_turn("Turn 120 -> 121 | Score: 52\n== Events == ..."))
    print("  HTTP timeout           ->", classify_end_turn("Error: timed out"))
    print("\nDry run complete — pipeline is coherent. "
          "(mock provider; set JEVCIV6_LLM_PROVIDER/jevciv6.toml for a real one)")


if __name__ == "__main__":
    main()
