"""Persistent FireTuner keeper client.

Holds ONE TCP connection open to the game's tuner port and polls the Lua
state list, logging whenever new states (especially InGame / GameCore_Tuner)
appear. Purpose: verify the hypothesis that Civ6 registers InGame Lua
contexts with the tuner only when a client is connected at context-creation
time (i.e. while the save loads).

Usage: python tuner_keeper.py [watch_seconds]
Prints JSONL events: {"t": ..., "event": "connected"|"states"|"found"|...}
Exits 0 as soon as InGame AND GameCore_Tuner are both visible.
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, "civ6-mcp/src")
from civ_mcp import tuner_client as tc  # noqa: E402


def emit(event: str, **kw):
    kw["t"] = round(time.time(), 1)
    kw["event"] = event
    print(json.dumps(kw, ensure_ascii=False), flush=True)


async def main() -> int:
    watch = float(sys.argv[1]) if len(sys.argv) > 1 else 1800.0
    deadline = time.time() + watch
    last_sig = None
    while time.time() < deadline:
        try:
            r, w = await tc.connect(timeout=4)
            emit("connected")
            while time.time() < deadline:
                try:
                    _, states = await tc.handshake(r, w)
                except Exception as e:
                    emit("handshake_err", err=str(e)[:80])
                    break
                names = [states[i + 1] for i in range(0, len(states) - 1, 2)]
                sig = "|".join(sorted(names))
                if sig != last_sig:
                    emit("states", n=len(names), new=[s for s in names if s not in (last_sig or "").split("|")])
                    last_sig = sig
                hits = [s for s in names if s in ("InGame", "GameCore_Tuner")]
                if len(hits) >= 2:
                    emit("FOUND_BOTH", states=hits)
                    w.close()
                    return 0
                await asyncio.sleep(6)
            try:
                w.close()
            except Exception:
                pass
        except Exception as e:
            emit("conn_err", err=str(e)[:80])
        await asyncio.sleep(4)
    emit("timeout")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
