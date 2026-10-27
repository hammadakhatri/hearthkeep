"""Encrypted secret store: a Fernet key in the OS keyring, values in a file on disk."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken
from keyring.errors import KeyringError

_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class SecretStoreError(Exception):
    """The store cannot be read: wrong or missing master key, or a corrupt file."""


class SecretStore:
    def __init__(self, data_dir: Path, service: str = "hearthkeep") -> None:
        self._dir = Path(data_dir) / "secrets"
        self._path = self._dir / "store.bin"
        self._service = service
        digest = hashlib.sha256(str(Path(data_dir).resolve()).encode()).hexdigest()[:16]
        self._key_user = f"master-key:{digest}"

    @staticmethod
    def _check_name(name: str) -> None:
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError("secret name must match [A-Za-z0-9_.-]{1,64}")

    def _fernet(self, create: bool) -> Fernet | None:
        try:
            key = keyring.get_password(self._service, self._key_user)
            if key is None:
                if not create:
                    return None
                key = Fernet.generate_key().decode()
                keyring.set_password(self._service, self._key_user, key)
        except KeyringError as exc:
            # A locked, missing or failing OS keyring backend.
            raise SecretStoreError("the OS keyring could not be used") from exc
        try:
            return Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise SecretStoreError("master key in the OS keyring is invalid") from exc

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        fernet = self._fernet(create=False)
        if fernet is None:
            raise SecretStoreError("master key is missing from the OS keyring")
        try:
            data = json.loads(fernet.decrypt(self._path.read_bytes()))
        except InvalidToken as exc:
            raise SecretStoreError("secret store cannot be decrypted (wrong key?)") from exc
        except ValueError as exc:
            raise SecretStoreError("secret store is corrupt") from exc
        if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()
        ):
            raise SecretStoreError("secret store is corrupt")
        return data

    def _save(self, data: dict[str, str]) -> None:
        fernet = self._fernet(create=True)
        token = fernet.encrypt(json.dumps(data).encode())
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".store-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(token)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def put(self, name: str, value: str) -> None:
        self._check_name(name)
        if not isinstance(value, str):
            raise TypeError("secret value must be a str")
        data = self._load()
        data[name] = value
        self._save(data)

    def get(self, name: str) -> str | None:
        self._check_name(name)
        return self._load().get(name)

    def delete(self, name: str) -> None:
        self._check_name(name)
        data = self._load()
        if name in data:
            del data[name]
            self._save(data)

    def names(self) -> list[str]:
        return sorted(self._load())
