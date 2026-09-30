"""ZCode PostToolUse hook: auto-journal civ6 MCP tool calls into the war-room.

Wiring (project .zcode/config.json):
{
  "hooks": {
    "enabled": true,
    "PostToolUse": [{
      "matcher": "mcp__civ6__.*",
      "hooks": [{"type": "command", "command": "python hooks/post_tool_use.py"}]
    }]
  }
}

Reads one hook-event JSON from stdin, best-effort maps it to /api/action.
Never raises: the game loop must not break because logging did.
"""
import json
import sys
import urllib.request

WAR_ROOM = "http://127.0.0.1:8080/api/action"


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return

    tool = event.get("tool_name") or ""
    if not tool.startswith("mcp__civ6__"):
        return
    payload = {
        "tool": tool.removeprefix("mcp__civ6__"),
        "args": event.get("tool_input") or {},
        "result": event.get("tool_response")
        if isinstance(event.get("tool_response"), (str, dict, list))
        else str(event.get("tool_response")),
        "meta": {"source": "zcode-hook"},
    }
    try:
        req = urllib.request.Request(
            WAR_ROOM,
            data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


if __name__ == "__main__":
    main()
