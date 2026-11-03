"""Setup.cmd: the double-click entry point that installs uv and runs `hearthkeep setup`."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CMD = REPO_ROOT / "Setup.cmd"


def test_setup_cmd_is_crlf_ascii():
    raw = CMD.read_bytes()
    assert raw.isascii() and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_setup_cmd_pins_and_checks_the_uv_installer():
    text = CMD.read_text()
    assert re.search(r'^set "UV_VERSION=\d+\.\d+\.\d+"$', text, re.M)
    assert re.search(r'^set "UV_INSTALLER_SHA256=[0-9A-F]{64}"$', text, re.M)
    assert "https://astral.sh/uv/'+$env:UV_VERSION+'/install.ps1" in text
    assert "Get-FileHash -Algorithm SHA256" in text
    assert "UV_NO_MODIFY_PATH" in text
    assert r"System32\WindowsPowerShell\v1.0\powershell.exe" in text
    # Started from a PowerShell 7 window, Windows PowerShell inherits a module path
    # that hides Get-FileHash, so the checksum step would fail.
    assert re.search(r'^set "PSModulePath="$', text, re.M)
    for banned in ("iex", "Invoke-Expression", "Set-ExecutionPolicy", "runas", "-Verb"):
        assert banned not in text


def test_setup_cmd_runs_setup_and_keeps_the_window_open():
    text = CMD.read_text()
    assert '"%UV_EXE%" run --project server hearthkeep setup' in text
    assert text.rstrip().endswith("pause >nul")
    assert "Press any key to close this window." in text


def test_gitattributes_keeps_cmd_crlf():
    assert "*.cmd text eol=crlf" in (REPO_ROOT / ".gitattributes").read_text()
