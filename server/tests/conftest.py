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
def memory_keyring():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)
