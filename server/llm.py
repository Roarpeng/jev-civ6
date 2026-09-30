# -*- coding: utf-8 -*-
"""Provider-agnostic LLM judgment layer ("Jev engine").

One call shape for every provider:

    judge(state, questions, model=None) -> (response_dict, latency_ms)

`response_dict` stays TypeSafe-compatible so the rest of the app (journal,
UI, executors) is provider-agnostic:

    {
      "model": "<model that answered>",
      "answers": {
         "<qid>": {"type": "choice", "choice": "...", "confidence": 0.9,
                    "probabilities": {...}}          # choice
                | {"type": "noul", "noul": 0.65}     # probability of "true"
                | {"type": "score", "score": 3, "probabilities": {...}}
      },
      "usage": {"input_tokens": N, "output_tokens": M}
    }

Providers:
  - typesafe : the original TypeSafe System One "Jev" API (api.typesafe.ai)
  - openai   : any OpenAI-compatible /chat/completions endpoint
               (OpenAI, DeepSeek, Moonshot, Qwen, Ollama, vLLM, LM Studio, ...)
  - anthropic: Anthropic Messages API
  - mock     : deterministic answerer for tests / dry runs

Selection & credentials come from server/config.py (jevciv6.toml + env).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from . import config as cfgmod
from .typesafe import system_one as _typesafe_call

_SYSTEM_PROMPT = (
    "You are Jev, a Civilization VI grand-strategy judgment model. "
    "You receive a JSON object with keys `state` (the empire, map, threats "
    "and available options) and `questions` (one decision per key). "
    "For EVERY question, decide strictly according to its `type`: a "
    "`choice` question asks you to pick exactly one key from its `criteria`; "
    "a `noul` question asks for the probability (0..1) that the statement "
    "in its `instructions` is true. "
    "Ground every judgment in concrete numbers, unit positions and tile "
    "coordinates found in `state`; do not invent facts. "
    "Reply with ONLY a JSON object, no prose, in exactly this shape: "
    '{"answers": {"<qid>": {"type": "choice", "choice": "<criteria key>", '
    '"confidence": <0..1>, "probabilities": {"<key>": <p>, ...}}, '
    '"<qid2>": {"type": "noul", "noul": <0..1>}}}'
)


def judge(state, questions: dict, model: str | None = None, cfg=None):
    """Dispatch to the configured provider. Returns (response, latency_ms)."""
    cfg = cfg if cfg is not None else cfgmod.load()
    provider = (cfg.llm.provider or "typesafe").lower()
    if provider == "typesafe":
        return _judge_typesafe(cfg, state, questions, model)
    if provider == "openai":
        return _judge_openai(cfg, state, questions, model)
    if provider == "anthropic":
        return _judge_anthropic(cfg, state, questions, model)
    if provider == "mock":
        return _judge_mock(state, questions, model)
    raise RuntimeError(f"unknown llm.provider: {provider!r}")


# ---------------------------------------------------------------- typesafe

def _judge_typesafe(cfg, state, questions, model):
    name = model or cfg.llm.model or "jev-latest"
    return _typesafe_call(
        state, questions, model=name, timeout=cfg.llm.timeout_s,
        api_key=cfgmod.llm_api_key(cfg) or None,
    )


# ---------------------------------------------------------------- helpers

def _post_json(url: str, payload: dict, headers: dict, timeout: float,
               retries: int = 3):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
        start = time.monotonic()
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r), int((time.monotonic() - start) * 1000)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last = RuntimeError(f"HTTP {e.code}: {detail}")
            if e.code in (429, 500, 502, 503, 504, 529) and attempt < retries:
                time.sleep(2 ** attempt * 1.5)
                continue
            raise last from e
        except urllib.error.URLError as e:
            last = RuntimeError(f"network error: {e}")
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise last from e
    raise last or RuntimeError("request failed")


def _extract_json(text: str) -> dict:
    """Tolerant JSON extraction from an LLM reply."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        return json.loads(t[i:j + 1])
    raise ValueError("no JSON object found in LLM reply")


def _build_user_prompt(state, questions) -> str:
    return json.dumps({"state": state, "questions": questions},
                      ensure_ascii=False, indent=1)


def _normalize_answers(questions: dict, raw: dict) -> dict:
    """Keep only valid answers; fill in `type`; validate choice keys."""
    answers_in = (raw or {}).get("answers") or {}
    out: dict = {}
    for qid, spec in (questions or {}).items():
        ans = answers_in.get(qid)
        if not isinstance(ans, dict):
            continue
        qtype = (spec or {}).get("type", "choice")
        ans.setdefault("type", qtype)
        if qtype == "choice":
            crit = (spec or {}).get("criteria") or {}
            choice = ans.get("choice")
            probs = ans.get("probabilities")
            if choice not in crit and isinstance(probs, dict) and probs:
                valid = {k: v for k, v in probs.items() if k in crit}
                if valid:
                    choice = max(valid, key=valid.get)
            if choice not in crit:
                continue  # unusable answer — drop it
            ans["choice"] = choice
        elif qtype == "noul":
            try:
                ans["noul"] = max(0.0, min(1.0, float(ans.get("noul"))))
            except (TypeError, ValueError):
                continue
        out[qid] = ans
    if questions and not out:
        raise ValueError("LLM returned no usable answers: " + json.dumps(raw)[:300])
    return {"answers": out}


# ---------------------------------------------------------------- openai

def _judge_openai(cfg, state, questions, model):
    llm = cfg.llm
    base = (llm.base_url or "https://api.openai.com/v1").rstrip("/")
    name = model or llm.model
    if not name:
        raise RuntimeError("llm.model is not set (required for provider=openai)")
    key = cfgmod.llm_api_key(cfg)
    if not key:
        raise RuntimeError(
            "no API key found: set llm.api_key_env / OPENAI_API_KEY / JEVCIV6_LLM_API_KEY"
        )
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        **{str(k): str(v) for k, v in (llm.extra_headers or {}).items()},
    }
    payload = {
        "model": name,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(state, questions)},
        ],
    }
    if llm.temperature is not None:
        payload["temperature"] = llm.temperature
    body = dict(payload)
    body["response_format"] = {"type": "json_object"}
    try:
        resp, latency = _post_json(base + "/chat/completions", body, headers,
                                   llm.timeout_s, llm.max_retries)
    except RuntimeError as e:
        if "HTTP 400" in str(e):  # endpoint rejects response_format — retry plain
            resp, latency = _post_json(base + "/chat/completions", payload,
                                       headers, llm.timeout_s, llm.max_retries)
        else:
            raise
    content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    raw = _extract_json(content)
    answers = _normalize_answers(questions, raw)
    usage = resp.get("usage") or {}
    return {
        "model": resp.get("model") or name,
        "answers": answers["answers"],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) or 0,
            "output_tokens": usage.get("completion_tokens", 0) or 0,
        },
    }, latency


# ---------------------------------------------------------------- anthropic

def _judge_anthropic(cfg, state, questions, model):
    llm = cfg.llm
    base = (llm.base_url or "https://api.anthropic.com").rstrip("/")
    name = model or llm.model
    if not name:
        raise RuntimeError("llm.model is not set (required for provider=anthropic)")
    key = cfgmod.llm_api_key(cfg)
    if not key:
        raise RuntimeError(
            "no API key found: set llm.api_key_env / ANTHROPIC_API_KEY / JEVCIV6_LLM_API_KEY"
        )
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        **{str(k): str(v) for k, v in (llm.extra_headers or {}).items()},
    }
    payload = {
        "model": name,
        "max_tokens": 1500,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": _build_user_prompt(state, questions)}],
    }
    if llm.temperature is not None:
        payload["temperature"] = llm.temperature
    resp, latency = _post_json(base + "/v1/messages", payload, headers,
                               llm.timeout_s, llm.max_retries)
    blocks = resp.get("content") or []
    content = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    raw = _extract_json(content)
    answers = _normalize_answers(questions, raw)
    usage = resp.get("usage") or {}
    return {
        "model": resp.get("model") or name,
        "answers": answers["answers"],
        "usage": {
            "input_tokens": usage.get("input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0,
        },
    }, latency


# ---------------------------------------------------------------- mock

def _judge_mock(state, questions, model=None):
    answers = {}
    for qid, spec in (questions or {}).items():
        qtype = (spec or {}).get("type", "choice")
        if qtype == "choice":
            crit = list(((spec or {}).get("criteria") or {}).keys())
            if crit:
                answers[qid] = {
                    "type": "choice", "choice": crit[0], "confidence": 0.5,
                    "probabilities": {crit[0]: 0.5},
                }
        elif qtype == "noul":
            answers[qid] = {"type": "noul", "noul": 0.5}
    return ({"model": "mock", "answers": answers,
             "usage": {"input_tokens": 0, "output_tokens": 0}}, 0)
