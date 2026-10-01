# -*- coding: utf-8 -*-
"""Capture the screen (the Civ 6 window is normally fullscreen) to a PNG.

Used by the autopilot whenever it pauses on an error — the screenshot is the
visual evidence of what the game was showing at the decision failure.
Usage: python scripts/game_screenshot.py <output.png>
"""
import subprocess
import sys

PS_TEMPLATE = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""


def capture(path: str) -> bool:
    ps = PS_TEMPLATE.format(path=path.replace("'", "''"))
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "screenshot.png"
    ok = capture(out)
    print(("saved " if ok else "FAILED ") + out)
    sys.exit(0 if ok else 1)
