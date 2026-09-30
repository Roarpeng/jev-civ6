"""Decision gate: pure code rules that decide WHEN the LLM judge is worth calling.

The autopilot posts each fresh snapshot to POST /api/gate. The gate diffs it
against the previous snapshot and fires only on real decision points. When no
trigger fires, no judgment call is made at all — judgment budget is spent only
where a typed answer changes what the code does next.

Triggers (deterministic, zero API cost):
  research_idle   — no tech/civic progressing or just completed
  civic_idle      — no civic being progressed
  production_idle — a city has nothing in the build queue
  new_threat      — hostile unit sighted that was not in the previous snapshot
  settler_idle    — an idle settler exists AND settle candidates were provided
  policy_slot     — empty policy slot (flagged; resolvable by code, no question)

Question instructions refer to the judge-state object the autopilot sends
(state.situation / state.empire / state.threats / state.settle_candidates /
state.available) — keep those keys in sync with autopilot.build_judge_state().
"""
from __future__ import annotations


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
    """Return {should_ask, triggers, question_ids, questions, skip_reason}."""
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
                    "Which technology should be set now? Consider the empire in "
                    "`state.empire` and any threats in `state.threats`; the "
                    "criteria list contains every currently researchable tech."
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
                    "given the empire in `state.empire`? The criteria list "
                    "contains every currently available civic."
                ),
                "criteria": {
                    (c.get("id") if isinstance(c, dict) else c):
                    (c.get("desc") if isinstance(c, dict) else "")
                    for c in civics
                },
            }

    # 3. Cities with an empty production queue (options are per-city).
    prod_by_city = {
        entry.get("city_id"): entry
        for entry in (available.get("production_by_city") or [])
        if isinstance(entry, dict)
    }
    first_idle = True
    for c in (snapshot.get("cities") or []):
        if (c.get("production") or "").strip():
            continue
        entry = prod_by_city.get(c.get("city_id"))
        options = (entry or {}).get("options") or []
        if not options:
            continue
        triggers.append("production_idle")
        qid = "production_pick" if first_idle else f"production_pick:{c.get('city_id')}"
        first_idle = False
        questions[qid] = {
            "type": "choice",
            "city_id": c.get("city_id"),
            "instructions": (
                f"City `{c.get('name', '?')}` has NOTHING in production; every "
                "turn without a build order wastes its production. What should "
                "it build now, weighing growth, defense and the threats in "
                "`state.threats`?"
            ),
            "criteria": {
                (o.get("id") if isinstance(o, dict) else o):
                (o.get("desc") if isinstance(o, dict) else "")
                for o in options
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
                "New hostile units were just sighted (fresh entries in "
                "`state.threats`). Given our units in `state.empire.units` and "
                "the city defense situation, must military units react to the "
                "threat this turn instead of continuing their current orders?"
            ),
            "criteria": {
                "true": "Threat warrants diverting units toward it this turn",
                "false": "City defense or distance makes interception unnecessary now",
            },
        }

    # 5. Settle candidates were provided → where should the settler found?
    candidates = snapshot.get("settle_candidates") or []
    if candidates:
        triggers.append("settler_idle")
        questions["settle_pick"] = {
            "type": "choice",
            "instructions": (
                "Our settler should found the next city. Choose the best site "
                "from `state.settle_candidates` (fresh water, resources, "
                "defense, distance to the capital)."
            ),
            "criteria": {
                (f"{c.get('x')},{c.get('y')}" if isinstance(c, dict) else str(c)):
                (c.get("desc") if isinstance(c, dict) else "")
                for c in candidates
            },
        }

    # 6. Empty policy slot — code can often resolve it; flag only.
    notes = [str(n).lower() for n in (snapshot.get("notes") or [])]
    if any("policy slot" in n for n in notes):
        triggers.append("policy_slot")

    skip_reason = "no decision point in snapshot delta" if not questions else None
    return {
        "should_ask": bool(questions),
        "triggers": triggers,
        "question_ids": list(questions.keys()),
        "questions": questions,
        "skip_reason": skip_reason,
    }
