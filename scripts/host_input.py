"""Host-side game input helper (optional, tiny, no admin).

The war-room container drives Civ6 over FireTuner, but a few game phases
have NO Lua surface at all — notably the post-load leader intro card
("CONTINUE GAME"), where the tuner handshake returns zero states. The only
way past is a real key press on the host.

Run on the game host:  python scripts/host_input.py [port]
War-room reaches it at http://host.docker.internal:<port>/press

Requires pywin32 on the host:  pip install pywin32

API:
  POST /press  {"key": "ENTER"}       — foreground the game, send the key
  POST /click  {"x": int, "y": int}   — click at game-window-relative coords
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 14319
_LOCK = threading.Lock()


def _game_window():
    import win32gui
    matches = []

    def cb(hwnd, _):
        if "Civilization VI" in win32gui.GetWindowText(hwnd):
            matches.append(hwnd)
        return True
    win32gui.EnumWindows(cb, None)
    return matches[0] if matches else None


def _force_foreground(hwnd):
    """SetForegroundWindow is blocked by the Windows foreground lock when
    the caller is a background process — the classic workaround is a tap
    of the ALT key first (unlocks the lock), then two attempts."""
    import ctypes
    import win32gui
    user32 = ctypes.windll.user32
    user32.keybd_event(0x12, 0, 0, 0)   # ALT down
    user32.keybd_event(0x12, 0, 2, 0)   # ALT up
    win32gui.ShowWindow(hwnd, 9)
    for _ in range(3):
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.25)
    return win32gui.GetForegroundWindow() == hwnd


def _foreground_and_send(key: str) -> str:
    import win32com.client
    import win32gui
    hwnd = _game_window()
    if not hwnd:
        return "ERR: game window not found"
    _force_foreground(hwnd)
    time.sleep(0.35)
    with _LOCK:
        shell = win32com.client.Dispatch("WScript.Shell")
        mapping = {"ENTER": "{ENTER}", "SPACE": " ", "ESC": "{ESC}"}
        shell.SendKeys(mapping.get(key.upper(), key))
    return f"OK sent {key}"


def _click(x: int, y: int) -> str:
    import ctypes
    import win32gui
    hwnd = _game_window()
    if not hwnd:
        return "ERR: game window not found"
    _force_foreground(hwnd)
    time.sleep(0.35)
    rect = win32gui.GetWindowRect(hwnd)
    ctypes.windll.user32.SetCursorPos(rect[0] + x, rect[1] + y)
    time.sleep(0.2)
    ctypes.windll.user32.mouse_event(2, 0, 0, 0, 0)
    ctypes.windll.user32.mouse_event(4, 0, 0, 0, 0)
    return f"OK clicked {rect[0] + x},{rect[1] + y}"


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/press":
                result = _foreground_and_send(payload.get("key", "ENTER"))
            elif self.path == "/click":
                result = _click(int(payload["x"]), int(payload["y"]))
            else:
                return self._json(404, {"error": "unknown path"})
            self._json(200, {"result": result})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": str(e)})

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"[host_input] 0.0.0.0:{LISTEN_PORT} — /press /click", flush=True)
    server.serve_forever()
