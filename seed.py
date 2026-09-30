"""Seed the war-room journal with the real opening moves of this campaign.

Data source: the actual T12 judgment call and the actions executed through the
civ6 MCP tools (research set, scout moves, end turn, T15 barbarian sighting).

Run while the server is up:  python seed.py
Idempotent: refuses to run if the journal already has events (pass --force).
"""
import argparse
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8080"
ART = "artifacts"


def post(path: str, payload: dict) -> None:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def spaced(event_id: str) -> None:
    """Tiny pause so seeded timestamps stay in reading order."""
    print(f"  + {event_id}")
    time.sleep(0.15)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    with urllib.request.urlopen(BASE + "/api/stats", timeout=5) as r:
        stats = json.load(r)
    if stats["jev"] + stats["actions"] + stats["states"] > 0 and not args.force:
        sys.exit("Journal already has events — use --force to seed anyway.")

    t12_req = json.load(open(f"{ART}/turn12_request.json", encoding="utf-8"))
    t12_resp = json.load(open(f"{ART}/turn12_answers.json", encoding="utf-8"))

    # T12 snapshot (composed from MCP outputs at judge time)
    post("/api/state", {
        "turn": 12,
        "snapshot": {
            "turn": 12, "civ": "Scythia", "leader": "Tomyris", "difficulty": "Prince",
            "score": 9,
            "yields": {"gold": 70, "gold_per_turn": 6, "science": 3.0,
                       "culture": 3.6, "faith": 0},
            "research": {"name": "NOTHING (idle!)", "turns_left": None, "progress_pct": 0},
            "civic": {"name": "Code of Laws", "turns_left": 2},
            "cities": [{"name": "Pokrovka", "pop": 2, "growth": "21t (SLOW)",
                        "production": "UNIT_SETTLER (7t)"}],
            "units": [{"type": "Warrior (CS 20)", "at": [51, 13], "moves": "2/2"}],
            "threats": [],
            "notes": ["No barbarians or civs sighted yet", "2% land explored",
                      "Unimproved: IVORY, COTTON, NITER in range"],
        },
    })
    spaced("state T12")

    # The real T12 judgment call
    post("/api/jev/record", {
        "request": {"state": t12_req["state"], "questions": t12_req["questions"],
                    "model": t12_req["model"]},
        "response": t12_resp,
        "latency_ms": 2400,
        "turn": 12,
        "meta": {"title": "T12 开局判断 · 6 parallel questions"},
    })
    spaced("jev T12 (6 questions)")

    # Actions executed from the judgment
    post("/api/action", {"turn": 12, "tool": "set_research",
                         "args": {"tech": "TECH_POTTERY"},
                         "result": "RESEARCHING|TECH_POTTERY  (Jev conf 0.99)"})
    spaced("action set_research")
    post("/api/action", {"turn": 12, "tool": "unit_action.move",
                         "args": {"unit": 131073, "to": [49, 13]},
                         "result": "MOVING_TO|49,13|BLOCKED (intermediate hills)"})
    spaced("action move (blocked)")
    post("/api/action", {"turn": 12, "tool": "unit_action.move",
                         "args": {"unit": 131073, "to": [50, 13]},
                         "result": "moved to hill (50,13) — revealed 9 tiles: "
                                   "Marble (50,11), Ivory (50,15)"})
    spaced("action move (scouted)")

    # T15 snapshot: after end_turn processed two turns
    post("/api/state", {
        "turn": 15,
        "snapshot": {
            "turn": 15, "civ": "Scythia", "leader": "Tomyris", "difficulty": "Prince",
            "score": 14,
            "yields": {"gold": 70, "gold_per_turn": 6, "science": 3.0,
                       "culture": 3.6, "faith": 0},
            "research": {"name": "Pottery", "turns_left": 5, "progress_pct": 40},
            "civic": None,
            "cities": [{"name": "Pokrovka", "pop": 2, "growth": "18t (SLOW)",
                        "production": "UNIT_SETTLER (7t)"}],
            "units": [{"type": "Warrior (CS 20)", "at": [50, 13], "moves": "2/2"}],
            "threats": [{"type": "Barbarian Scout", "at": [54, 11], "cs": 10}],
            "notes": ["Action required: fill policy slot",
                      "Action required: choose civic (Code of Laws done)",
                      "New reveals: HORSES (55,14), STONE (55,12)"],
        },
    })
    spaced("state T15 (barbarian!)")

    print("Seeded. Open http://127.0.0.1:8080")


if __name__ == "__main__":
    main()
