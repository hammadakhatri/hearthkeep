"""The Start menu shortcut that opens Hearthkeep: `pythonw -m hearthkeep open --data "<folder>"`.

It is made by PowerShell from System32 running a fixed script. The paths travel in
environment variables, so nothing the owner chose is ever pasted into code."""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

from hearthkeep import procutil
from hearthkeep.startup import pythonw_path, quote_arg

log = logging.getLogger(__name__)

SHORTCUT_NAME = "Hearthkeep.lnk"
SHORTCUT_FAILED = "The Start menu shortcut couldn't be made."
POWERSHELL_TIMEOUT_S = 30
_POWERSHELL = r"WindowsPowerShell\v1.0\powershell.exe"
SHORTCUT_SCRIPT = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:HK_LNK); "
    "$s.TargetPath = $env:HK_TARGET; $s.Arguments = $env:HK_ARGS; "
    "$s.WorkingDirectory = $env:HK_DIR; $s.Description = 'Open Hearthkeep'; $s.Save()"
)

Run = Callable[..., subprocess.CompletedProcess]


class ShortcutError(Exception):
    """The shortcut could not be made; the message is for the owner."""


def shortcut_path() -> Path:
    r"""`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Hearthkeep.lnk`."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ShortcutError(SHORTCUT_FAILED)
    folder = PureWindowsPath(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
    return Path(str(folder / SHORTCUT_NAME))


def powershell_exe() -> str:
    """powershell.exe from System32, never whatever PATH finds first."""
    exe = procutil.system32_exe(_POWERSHELL)
    if exe is None:
        raise ShortcutError(SHORTCUT_FAILED)
    return str(exe)


def install_shortcut(data_dir: Path, *, run: Run = subprocess.run, pythonw: Path | None = None):
    """Make (or replace) the Start menu shortcut for this data folder."""
    data = str(data_dir)
    env = {
        **os.environ,
        "HK_LNK": str(shortcut_path()),
        "HK_TARGET": str(pythonw or pythonw_path()),
        "HK_ARGS": f"-m hearthkeep open --data {quote_arg(data)}",
        "HK_DIR": data,
    }
    argv = [
        powershell_exe(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        SHORTCUT_SCRIPT,
    ]
    try:
        result = run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=POWERSHELL_TIMEOUT_S,
            check=False,
            creationflags=procutil.NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Start menu shortcut failed: %s", type(exc).__name__)
        raise ShortcutError(SHORTCUT_FAILED) from None
    if result.returncode != 0:
        log.warning("Start menu shortcut failed: exit code %s", result.returncode)
        raise ShortcutError(SHORTCUT_FAILED)
