# -*- coding: utf-8 -*-
"""Standalone judgment CLI — works with ANY configured LLM provider.

Usage:
    python jev_judge.py request.json [answers.json]
    python jev_judge.py request.json --provider openai --model gpt-4o-mini
    python jev_judge.py request.json --provider mock        # offline dry run

The request JSON has the same shape the war-room uses:
    {"state": {...}, "questions": {...}, "model": "..."}

Configuration comes from jevciv6.toml / environment (see server/config.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `server` importable

from server import config as cfgmod  # noqa: E402
from server.llm import judge  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="TypeSafe/OpenAI/Anthropic judgment CLI")
    ap.add_argument("request", help="path to request JSON (state + questions)")
    ap.add_argument("out", nargs="?", help="optional output path for the answers JSON")
    ap.add_argument("--provider", help="override llm.provider (typesafe|openai|anthropic|mock)")
    ap.add_argument("--model", help="override model name")
    ap.add_argument("--config", help="path to jevciv6.toml (default: project root)")
    args = ap.parse_args()

    with open(args.request, encoding="utf-8") as f:
        payload = json.load(f)
    state = payload.get("state")
    questions = payload.get("questions") or {}
    if not questions:
        sys.exit("request JSON must contain a non-empty 'questions' object")

    cfg = cfgmod.load(args.config)
    if args.provider:
        cfg.llm.provider = args.provider
    model = args.model or payload.get("model") or None

    resp, latency_ms = judge(state, questions, model=model, cfg=cfg)
    out = json.dumps(resp, indent=1, ensure_ascii=False)
    print(out)
    print(f"\n[provider={cfg.llm.provider} model={resp.get('model')} latency={latency_ms}ms]",
          file=sys.stderr)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(resp, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
