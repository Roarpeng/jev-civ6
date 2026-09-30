"""TypeSafe System One (Jev) HTTP client with retry.

Auth: TYPESAFE_API_KEY from the environment. On Windows, if the variable is
not visible in the current process (common when the shell predates the
variable), fall back to the user-level registry environment — read once,
cached. The key is never logged.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://api.typesafe.ai/v1/systemone"

_fallback_key: str | None = None


def _api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    global _fallback_key
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    if _fallback_key is None and sys.platform == "win32":
        try:
            import subprocess

            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Environment]::GetEnvironmentVariable('TYPESAFE_API_KEY','User')"],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            _fallback_key = out or ""
        except Exception:
            _fallback_key = ""
    if not _fallback_key:
        raise RuntimeError(
            "TYPESAFE_API_KEY not found in process env or Windows user env"
        )
    return _fallback_key


def system_one(
    state,
    questions: dict,
    model: str = "jev-latest",
    timeout: float = 60.0,
    api_key: str | None = None,
) -> tuple[dict, int]:
    """Call the System One API. Returns (response_dict, latency_ms)."""
    body = json.dumps(
        {"state": state, "model": model, "questions": questions}
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {_api_key(api_key)}",
        "Content-Type": "application/json",
    }
    last_err: Exception | None = None
    for attempt in range(4):
        start = time.monotonic()
        try:
            req = urllib.request.Request(API_URL, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.load(r)
            return resp, int((time.monotonic() - start) * 1000)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 529) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                last_err = RuntimeError(f"HTTP {e.code}: {detail}")
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                last_err = e
                continue
            raise RuntimeError(f"network error: {e}") from e
    raise last_err or RuntimeError("system_one failed")
