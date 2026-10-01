"""Spawn the civ6-mcp bridge the same way the war-room does (stdin=PIPE,
never written) but with stderr/stdout redirected to files for debugging.

Usage: python scripts/spawn_bridge_debug.py [out_prefix]
"""
import subprocess
import sys
import time

prefix = sys.argv[1] if len(sys.argv) > 1 else "bridge"
proc = subprocess.Popen(
    [sys.executable, "-m", "civ_mcp"],
    stdin=subprocess.PIPE,
    stdout=open(f"{prefix}_out.log", "wb"),
    stderr=open(f"{prefix}_err.log", "wb"),
    cwd=r"C:\Users\roarp\Desktop\TMP\Code\AICode\jev-civ6",
)
print(f"bridge pid={proc.pid} logs={prefix}_err.log", flush=True)
try:
    while proc.poll() is None:
        time.sleep(5)
except KeyboardInterrupt:
    proc.terminate()
print(f"bridge exited rc={proc.returncode}", flush=True)
