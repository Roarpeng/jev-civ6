"""Thin TypeSafe judgment engine: POST a state+questions JSON to Jev, print typed answers.

Usage: python jev_judge.py request.json [answers.json]
Reads TYPESAFE_API_KEY from environment.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"


def main() -> None:
    req_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    with open(req_path, encoding="utf-8") as f:
        payload = json.load(f)
    payload.setdefault("model", "jev-latest")

    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        sys.exit("TYPESAFE_API_KEY not set")

    body = json.dumps(payload).encode("utf-8")
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                API_URL,
                data=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")

    out = json.dumps(resp, indent=1, ensure_ascii=False)
    print(out)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(resp, f, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
