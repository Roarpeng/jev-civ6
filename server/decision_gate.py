"""Decision gate: pure code rules that decide WHEN Jev is worth calling.

The orchestrator posts each fresh snapshot to POST /api/gate. The gate diffs it
against the previous snapshot and fires only on real decision points. When no
trigger fires, no Jev call is made at all — judgment budget is spent only where
a typed answer changes what the code does next.

Triggers (deterministic, zero API cost):
  research_idle   — no tech/civic progressing or just completed
  civic_idle      — no civic being progressed
  production_idle — a city has nothing in the build queue
  new_threat      — enemy/barbarian unit sighted that was not in the previous snapshot
  settler_idle    — a settler stands idle (site choice needed)
  policy_slot     — empty policy slot (flagged; resolvable by code, no question)
"""
from typing import Any


def _is_idle(name, turns_left) -> bool:
    """True when nothing is progressing. The game reports 'no research' as
    the literal string 'None' with turns -1 (also '' / NOTHING happen)."""
    n = str(name or "").strip().upper()
    if n in ("", "NONE", "NOTHING", "NULL"):
        return True
    return turns_left in (0, -1)


def _research_idle(snapshot: dict) -> bool:
    res = snapshot.get("research") or {}
    return _is_idle(res.get("name"), res.get("turns_left"))


def _civic_idle(snapshot: dict) -> bool:
    civ = snapshot.get("civic")
    if not civ:
        return True
    if isinstance(civ, dict):
        return _is_idle(civ.get("name"), civ.get("turns_left"))
    return False


def _threat_key(t: dict) -> tuple:
    at = t.get("at") or [None, None]
    return (str(t.get("type")), str(at[0]), str(at[1]))


def evaluate(snapshot: dict, prev_snapshot: dict | None) -> dict:
    triggers: list[str] = []
    questions: dict[str, dict] = {}
    available = snapshot.get("available") or {}

    # 1. Research idle → pick a tech from what's actually researchable.
    if _research_idle(snapshot):
        techs = available.get("techs") or []
        if techs:
            triggers.append("research_idle")
            questions["research_pick"] = {
                "type": "choice",
                "instructions": (
                    "No technology is being researched, so all science is wasted. "
                    "Which technology should be set now, given the empire state in "
                    "`empire` and the map in `terrain`?"
                ),
                "criteria": {
                    (t.get("id") if isinstance(t, dict) else t):
                    (t.get("desc") if isinstance(t, dict) else "")
                    for t in techs
                },
            }

    # 2. Civic idle → pick a civic.
    if _civic_idle(snapshot):
        civics = available.get("civics") or []
        if civics:
            triggers.append("civic_idle")
            questions["civic_pick"] = {
                "type": "choice",
                "instructions": (
                    "No civic is being progressed. Which civic should be set now, "
                    "considering `focus` and current military/economic needs?"
                ),
                "criteria": {
                    (c.get("id") if isinstance(c, dict) else c):
                    (c.get("desc") if isinstance(c, dict) else "")
                    for c in civics
                },
            }

    # 3. A city with an empty production queue.
    idle_cities = [
        c for c in (snapshot.get("cities") or [])
        if not (c.get("production") or "").strip()
    ]
    prod_options = available.get("production_options") or []
    if idle_cities and prod_options:
        city = idle_cities[0]
        triggers.append("production_idle")
        questions["production_pick"] = {
            "type": "choice",
            "instructions": (
                f"City `{city.get('name', '?')}` has nothing in production. "
                "What should it build, considering growth, defense and `focus`?"
            ),
            "criteria": {
                (o.get("id") if isinstance(o, dict) else o):
                (o.get("desc") if isinstance(o, dict) else "")
                for o in prod_options
            },
        }

    # 4. New threat vs previous snapshot → is a response needed?
    cur = {_threat_key(t) for t in (snapshot.get("threats") or [])}
    prev = {_threat_key(t) for t in ((prev_snapshot or {}).get("threats") or [])}
    fresh = cur - prev
    if fresh:
        triggers.append("new_threat")
        questions["threat_response"] = {
            "type": "noul",
            "instructions": (
                "A new hostile unit was sighted (see `threats` — the fresh entries "
                "were not present last check). Given `units` and city defense, must "
                "military units react this turn instead of their current orders?"
            ),
            "criteria": {
                "true": "Threat warrants diverting units toward it this turn",
                "false": "City defense or distance makes interception unnecessary now",
            },
        }

    # 5. Idle settler → where to settle (only if candidates were provided).
    idle_settler = any(
        "SETTLER" in str(u.get("type", "")).upper()
        for u in (snapshot.get("units") or [])
    )
    candidates = snapshot.get("settle_candidates") or []
    if idle_settler and candidates:
        triggers.append("settler_idle")
        questions["settle_pick"] = {
            "type": "choice",
            "instructions": (
                "A settler is idle. Choose the best site from `settle_candidates` "
                "(water, yields, distance, safety)."
            ),
            "criteria": {
                (c.get("id") if isinstance(c, dict) else str(c)):
                (c.get("desc") if isinstance(c, dict) else "")
                for c in candidates
            },
        }

    # 6. Empty policy slot — code can often resolve it; flag only.
    notes = [str(n).lower() for n in (snapshot.get("notes") or [])]
    if any("policy slot" in n for n in notes):
        triggers.append("policy_slot")

    skip_reason = (
        "no decision point in snapshot delta" if not questions
        else None
    )
    return {
        "should_ask": bool(questions),
        "triggers": triggers,
        "question_ids": list(questions.keys()),
        "questions": questions,
        "skip_reason": skip_reason,
    }
