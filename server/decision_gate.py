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
  attack_available — one of our military units has engine-verified targets
  settler_idle    — an idle settler exists AND settle candidates were provided
  policy_slot     — empty policy slot (flagged; resolvable by code, no question)

Question instructions refer to the judge-state object the autopilot sends
(state.situation / state.empire / state.threats / state.settle_candidates /
state.available) — keep those keys in sync with autopilot.build_judge_state().
"""
from __future__ import annotations

import re

# how many military units get a per-unit tactics question (bounds token cost)
TACTICS_UNIT_CAP = 8
# a unit below this HP fraction is offered a "retreat" option
RETREAT_HP_FRACTION = 0.65
# per-victory-path items worth naming explicitly in pick questions when they
# appear in the options (counteracts the "always build warriors" inertia)
STRATEGY_KEY_ITEMS = {
    "religion": {
        "items": ("DISTRICT_HOLY_SITE", "BUILDING_SHRINE", "BUILDING_TEMPLE",
                  "TECH_ASTROLOGY", "CIVIC_MYSTICISM"),
        "why": "Holy Site districts and shrines are the core of a religion "
               "victory (faith income → found religion → apostles)",
    },
    "science": {
        "items": ("DISTRICT_CAMPUS", "BUILDING_LIBRARY", "BUILDING_UNIVERSITY",
                  "TECH_WRITING", "TECH_EDUCATION"),
        "why": "Campus districts power the science victory",
    },
    "culture": {
        "items": ("DISTRICT_THEATER", "BUILDING_AMPHITHEATER",
                  "CIVIC_DRAMA_POETRY", "CIVIC_THEATER"),
        "why": "Theater Squares and great works drive a culture victory",
    },
    "domination": {
        "items": ("DISTRICT_ENCAMPMENT", "BUILDING_BARRACKS",
                  "TECH_BRONZE_WORKING", "TECH_IRON_WORKING"),
        "why": "Encampments and better units win wars",
    },
    "diplomatic": {
        "items": ("DISTRICT_GOVERNMENT", "BUILDING_FOREIGN_MINISTRY",),
        "why": "Government Plaza supports diplomacy",
    },
}


def _faith_lock_note(snapshot: dict, strategy: dict | None,
                     options: list) -> str:
    """Religion path: buying missionaries/apostles is BLOCKED until the Holy
    Site city has a Shrine (missionaries) / Temple (apostles). Make that
    decisive when those buildings are buildable now."""
    if not (strategy and strategy.get("path") == "religion"):
        return ""
    ids = {str(o.get("id") if isinstance(o, dict) else o) for o in options}
    if not (ids & {"BUILDING_SHRINE", "BUILDING_TEMPLE"}):
        return ""
    for c in (snapshot.get("cities") or []):
        if any("HOLY_SITE" in str(d) for d in (c.get("districts") or [])):
            b = {str(x).upper() for x in (c.get("buildings") or [])}
            if "BUILDING_SHRINE" not in b or "BUILDING_TEMPLE" not in b:
                return (" CRITICAL: faith purchase of missionaries/apostles "
                        "is BLOCKED until this city has a Shrine and Temple "
                        "— build them before anything else on this path.")
    return ""


def _strategy_key_note(strategy: dict | None, options: list) -> str:
    """If a strategy-critical item appears in this option list, say so."""
    if not (strategy and strategy.get("path")):
        return ""
    info = STRATEGY_KEY_ITEMS.get(strategy["path"])
    if not info:
        return ""
    ids = {str(o.get("id") if isinstance(o, dict) else o) for o in options}
    if ids & set(info["items"]):
        return (f" NOTE: an option above is CRITICAL for our "
                f"{strategy['path']} victory path — {info['why']}.")
    return ""


def _city_comfort_note(city: dict, n_cities: int) -> str:
    """Loyalty/amenity warnings + under-expansion alert so builds respond to
    unrest instead of ignoring it (a city already flipped free this game)."""
    note = ""
    loy = city.get("loyalty")
    if isinstance(loy, (int, float)) and loy < 70:
        note += (f" WARNING: loyalty {loy:g} ({city.get('loyalty_per_turn')}"
                 f"/turn) — without loyalty help this city may flip free!")
    am = city.get("amenities")
    pop = city.get("pop") or 1
    if isinstance(am, (int, float)) and am < (pop + 3) // 2:
        note += (f" Amenities low ({am:g} for pop {pop}) — entertainment "
                 f"buildings or luxury improvements needed.")
    hs = city.get("housing")
    if isinstance(hs, (int, float)) and hs <= pop:
        note += (f" Housing FULL ({hs:g}/{pop}) — growth is stalled; "
                 f"Granary/Aqueduct/neighbourhood or housing policies "
                 f"needed.")
    if n_cities < 3:
        note += (" NOTE: the empire has only "
                 f"{n_cities} city(ies) — a SETTLER is usually the highest-"
                 f"value build when defensible land remains.")
    return note


def _army_note(snapshot: dict) -> str:
    """Tell the judge how much military we already field, so it can stop
    pouring production into units when the homeland is already garrisoned."""
    mil = [u for u in (snapshot.get("units") or [])
           if (u.get("cs") or 0) > 0
           and "SETTLER" not in str(u.get("type", "")).upper()]
    threat_n = len(snapshot.get("threats") or [])
    note = f" Our military: {len(mil)} units; active threats: {threat_n}."
    if len(mil) >= 6 and threat_n == 0:
        note += (" With the homeland well defended, prefer economy, "
                 "science, faith or district builds over more units.")
    return note


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

    # 0a. Victory strategy — decide (or re-decide every 30 turns) the path
    #     every other choice is biased toward.
    strategy = snapshot.get("strategy") or None
    strategy_hint = ""
    if strategy and strategy.get("path"):
        strategy_hint = (f" Empire strategy: pursue a {strategy['path']} "
                         f"victory — bias this choice accordingly.")
    turn = snapshot.get("turn") or 0
    if (not strategy) or (turn - (strategy.get("since_turn") or 0) >= 30) \
            or (turn < (strategy.get("since_turn") or 0)):  # new campaign
        triggers.append("strategy_review")
        questions["strategy_pick"] = {
            "type": "choice",
            "instructions": (
                "Decide this empire's VICTORY PATH; every research, civic, "
                "production and military choice will be biased toward it for "
                "the next 30 turns. Judge feasibility from `state.empire` and "
                "`state.situation` — current yields, cities, army, gold."),
            "criteria": {
                "science": ("Science victory: Campus districts + Library → "
                            "space-race projects; needs sustained science"),
                "culture": ("Culture victory: Theater Squares, wonders, "
                            "great works, tourism; needs culture income"),
                "domination": ("Domination victory: capture every capital; "
                               "needs a strong modern army and production"),
                "religion": ("Religious victory: Holy Sites, faith income, "
                             "apostles; best when faith is already flowing "
                             "or the civ has faith synergy"),
                "diplomatic": ("Diplomatic victory: World Congress favors "
                               "and alliances; slow and situational"),
            },
        }

    # 0b. Policy cards — the game pre-fills defaults, so review periodically
    #     (every 15 turns) whether to swap for newly unlocked policies.
    pol = snapshot.get("policies") or {}
    opts_by = pol.get("options_by_slot") or {}
    if (pol.get("review_age") or 999) >= 15:
        for s in (pol.get("slots") or []):
            sid = str(s.get("slot_index"))
            opts = opts_by.get(sid) or {}
            if len(opts) < 2:
                continue
            triggers.append("policy_review")
            kind = str(s.get("slot_type") or "").replace("SLOT_", "")
            questions[f"policy_pick:{sid}"] = {
                "type": "choice",
                "slot_index": s.get("slot_index"),
                "instructions": (
                    f"Review the {kind} policy slot "
                    f"(government: {pol.get('government')}). Keep the "
                    f"current policy or swap for a better one given the "
                    f"empire's needs." + strategy_hint),
                "criteria": opts,
            }

    # 0c. Envoy tokens available → which city-state deserves them?
    cs = snapshot.get("city_states") or []
    if cs:
        triggers.append("envoy_available")
        questions["envoy_pick"] = {
            "type": "choice",
            "instructions": (
                "We have envoy tokens to assign. Pick the city-state to send "
                "one envoy to (suzerainty at 3+ envoys gives a unique bonus; "
                "types give empire-wide yields at 1/3/6 envoys)."
                + strategy_hint),
            "criteria": {
                str(s.get("player_id")): (
                    f"{s.get('name')} [{s.get('city_state_type')}] — our "
                    f"envoys: {s.get('envoys_sent')}; suzerain: "
                    f"{s.get('suzerain_name')}")
                for s in cs if s.get("player_id") is not None
            },
        }

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
                    + strategy_hint
                    + _strategy_key_note(strategy, techs)
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
                    + strategy_hint
                    + _strategy_key_note(strategy, civics)
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
    n_cities = len([c for c in (snapshot.get("cities") or [])
                    if c.get("city_id") is not None])
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
                "`state.threats`?" + strategy_hint
                + _strategy_key_note(strategy, options)
                + _faith_lock_note(snapshot, strategy, options)
                + _army_note(snapshot)
                + _city_comfort_note(c, n_cities)
            ),
            "criteria": {
                (o.get("id") if isinstance(o, dict) else o):
                (o.get("desc") if isinstance(o, dict) else "")
                for o in options
            },
        }

    # 4. Threats + idle military units → one tactical choice question PER UNIT.
    #    (v2.1: replaces the old single-bit `threat_response` noul — Jev now
    #    picks each unit's action from engine-verified options instead of the
    #    executor guessing after a yes/no coin flip.)
    cur = {_threat_key(t) for t in (snapshot.get("threats") or [])}
    prev = {_threat_key(t) for t in ((prev_snapshot or {}).get("threats") or [])}
    fresh = cur - prev
    threats = snapshot.get("threats") or []
    mil = [u for u in (snapshot.get("units") or [])
           if (u.get("cs") or 0) > 0 and (u.get("moves") or 0) > 0
           and "SETTLER" not in str(u.get("type", "")).upper()]
    attack_ready = any(u.get("targets") for u in mil)
    if fresh:
        triggers.append("new_threat")
    if attack_ready:
        triggers.append("attack_available")
    if threats and mil and (fresh or attack_ready):
        threat_by_at = {((t.get("at") or [None, None])[0],
                         (t.get("at") or [None, None])[1]): t for t in threats}
        nearest = min(threats, key=lambda t: t.get("distance") or 99)
        for u in mil[:TACTICS_UNIT_CAP]:
            crit: dict[str, str] = {}
            for tgt in (u.get("targets") or []):
                m = re.match(r"([A-Za-z_]+)@(-?\d+),(-?\d+)\((\d+)hp\)",
                             str(tgt).strip())
                if not m:
                    continue
                ttype, tx, ty, thp = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
                info = threat_by_at.get((tx, ty)) or {}
                crit[f"attack:{tx},{ty}"] = (
                    f"Attack the {ttype} at ({tx},{ty}) this turn — "
                    f"engine-verified reachable; enemy HP {thp}, "
                    f"CS {info.get('cs', '?')}")
            crit["advance"] = (
                f"Move toward the nearest threat {nearest.get('type')} at "
                f"{nearest.get('at')} (distance {nearest.get('distance')}, "
                f"HP {nearest.get('hp')}, CS {nearest.get('cs')})")
            crit["fortify"] = ("Hold position and fortify — +defence and the "
                               "unit heals while idle")
            hp, max_hp = u.get("hp"), u.get("max_hp")
            if hp is not None and max_hp and hp < RETREAT_HP_FRACTION * max_hp:
                crit["retreat"] = ("Unit is badly wounded — fall back toward "
                                   "our city to heal instead of fighting")
            questions[f"tactics:{u.get('unit_index')}"] = {
                "type": "choice",
                "unit_index": u.get("unit_index"),
                "instructions": (
                    f"Our {u.get('type')} (CS {u.get('cs')}, HP "
                    f"{hp if hp is not None else '?'}/"
                    f"{max_hp if max_hp is not None else '?'}) at {u.get('at')} "
                    f"needs orders now; hostiles are in `state.threats`. Pick "
                    f"this unit's action — favour finishing wounded enemies "
                    f"and favourable fights; do not trade a healthy unit for "
                    f"nothing; retreat only when badly hurt."),
                "criteria": crit,
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
                "defense, distance to the capital)." + strategy_hint
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
