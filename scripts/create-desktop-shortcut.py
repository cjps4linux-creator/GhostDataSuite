import os
from pathlib import Path

import winshell
from win32com.client import Dispatch

# Paths are derived from this file's location so the script works from any
# checkout. Override the project root with GHOST_PROJECT_DIR if needed.
APP_DIR = Path(os.environ.get("GHOST_PROJECT_DIR", Path(__file__).resolve().parent.parent))
LAUNCHER = APP_DIR / "scripts" / "launch-dashboard.vbs"

desktop = winshell.desktop()
path = os.path.join(desktop, "Ghost-Data Suite.lnk")
shell = Dispatch('WScript.Shell')
shortcut = shell.CreateShortCut(path)
shortcut.Targetpath = str(LAUNCHER)
shortcut.WorkingDirectory = str(APP_DIR)
shortcut.IconLocation = f"{LAUNCHER},0"
shortcut.save()

print(f"Created shortcut: {path}")
print(f"  target: {LAUNCHER}")
