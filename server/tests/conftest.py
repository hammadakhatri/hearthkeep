import os
import sys
from pathlib import Path

# The repo-level scripts/ folder is not a package; make its modules importable in tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import keyring
import keyring.backend
import pytest


class MemoryKeyring(keyring.backend.KeyringBackend):
    """In-memory keyring so tests never touch the real OS keyring."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture(autouse=True)
def system_root(monkeypatch):
    """Start-at-login tests fake Windows; give schtasks a System32 folder on any OS (CI is
    Linux). Never runs anything there: those tests replace schtasks with a fake."""
    if not (sys.platform == "win32" or "SystemRoot" in os.environ):
        monkeypatch.setenv("SystemRoot", "C:\\Windows")


@pytest.fixture
def pythonw_beside_python(tmp_path, monkeypatch):
    """`startup enable` looks for pythonw.exe beside this Python; give it one on any OS."""
    python = tmp_path / "python" / "python.exe"
    python.parent.mkdir()
    (python.parent / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(python))


@pytest.fixture(autouse=True)
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def hermetic_addon_probes(monkeypatch):
    """The add-on registry never probes the real PC in tests: no Tailscale, no schtasks, and
    Docker counts as not installed. Tests of `tailscale_state` call it with their own fake."""
    from hearthkeep import addons
    from hearthkeep.startup import StartupStatus

    monkeypatch.setattr(
        addons, "tailscale_state", lambda *a, **k: addons.TailscaleState(False, False, None)
    )
    monkeypatch.setattr(
        addons, "startup_status", lambda fresh=False: StartupStatus(False, False, None)
    )
    monkeypatch.setattr(addons, "docker_installed", lambda: False)


@pytest.fixture(autouse=True)
def hermetic_cli_host(monkeypatch):
    """`hearthkeep serve` runs the upgrade, which on Windows would read and write the real
    data-folder record, start-at-login task and Start menu shortcut. In tests the CLI's own
    host is the unsupported one (as on Linux CI); tests that need Windows pass a fake host."""
    from hearthkeep import cli
    from hearthkeep.host import UnsupportedHost

    monkeypatch.setattr(cli, "current_host", UnsupportedHost)


@pytest.fixture
def sandbox_image():
    """The built sandbox image's tag; skips when Docker or the image is missing."""
    from hearthkeep.sandbox.image import docker_ok, image_present, image_tag

    if not docker_ok():
        pytest.skip("Docker is not running")
    tag = image_tag()
    if not image_present(tag):
        pytest.skip(f"sandbox image {tag} is not built (hearthkeep sandbox build --data <data>)")
    return tag


@pytest.fixture(autouse=True)
def demo_level(request, monkeypatch):
    """The demo tools (tests/demo_tools.py) share a `demo` level that no longer ships: give it
    its old default here. Tests marked `real_levels` see the real table."""
    if request.node.get_closest_marker("real_levels"):
        return
    from hearthkeep import permissions

    monkeypatch.setitem(permissions.DEFAULT_LEVELS, "demo", permissions.ToolLevel.ALLOWED)
