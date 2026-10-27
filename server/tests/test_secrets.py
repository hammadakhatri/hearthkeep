import pytest
from cryptography.fernet import Fernet

from hearthkeep.secrets import SecretStore, SecretStoreError


def test_put_get_roundtrip(tmp_path):
    s = SecretStore(tmp_path)
    s.put("chat_api_key", "supersecret")
    assert s.get("chat_api_key") == "supersecret"
    assert SecretStore(tmp_path).get("chat_api_key") == "supersecret"


def test_file_does_not_contain_plaintext(tmp_path):
    SecretStore(tmp_path).put("k", "supersecret")
    assert b"supersecret" not in (tmp_path / "secrets" / "store.bin").read_bytes()


def test_get_missing_returns_none(tmp_path):
    assert SecretStore(tmp_path).get("nope") is None


def test_delete_and_names(tmp_path):
    s = SecretStore(tmp_path)
    s.put("b", "2")
    s.put("a", "1")
    assert s.names() == ["a", "b"]
    s.delete("a")
    assert s.get("a") is None
    assert s.names() == ["b"]
    s.delete("a")  # deleting a missing name is a no-op


def test_wrong_key_raises(tmp_path, memory_keyring):
    s = SecretStore(tmp_path)
    s.put("k", "supersecret")
    for key in list(memory_keyring.store):
        memory_keyring.store[key] = Fernet.generate_key().decode()
    with pytest.raises(SecretStoreError) as exc:
        s.get("k")
    assert "supersecret" not in str(exc.value)


def test_missing_key_with_existing_store_raises(tmp_path, memory_keyring):
    SecretStore(tmp_path).put("k", "v")
    memory_keyring.store.clear()
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path).get("k")


def test_corrupt_store_raises(tmp_path):
    s = SecretStore(tmp_path)
    s.put("k", "v")
    (tmp_path / "secrets" / "store.bin").write_bytes(b"garbage")
    with pytest.raises(SecretStoreError):
        s.names()


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "../x", "a/b", "é"])
def test_invalid_names_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        SecretStore(tmp_path).put(bad, "v")


def test_keys_scoped_per_data_dir(tmp_path, memory_keyring):
    SecretStore(tmp_path / "one").put("k", "v")
    SecretStore(tmp_path / "two").put("k", "v")
    assert len(memory_keyring.store) == 2


def test_no_temp_files_left(tmp_path):
    SecretStore(tmp_path).put("k", "v")
    assert [p.name for p in (tmp_path / "secrets").iterdir()] == ["store.bin"]


@pytest.mark.parametrize("bad", [None, 5, b"x", ["x"]])
def test_non_str_value_rejected_and_store_intact(tmp_path, bad):
    s = SecretStore(tmp_path)
    s.put("good", "v")
    with pytest.raises(TypeError):
        s.put("k", bad)
    assert s.get("good") == "v"
    assert s.names() == ["good"]


@pytest.mark.parametrize("bad", ["", "a b", "x" * 65, "../x"])
def test_invalid_names_rejected_on_get_and_delete(tmp_path, bad):
    s = SecretStore(tmp_path)
    with pytest.raises(ValueError):
        s.get(bad)
    with pytest.raises(ValueError):
        s.delete(bad)


def test_keyring_backend_errors_become_secret_store_errors(tmp_path, memory_keyring, monkeypatch):
    from keyring.errors import KeyringError, KeyringLocked

    SecretStore(tmp_path).put("k", "supersecret")

    def broken(*args):
        raise KeyringLocked("the keyring is locked")

    monkeypatch.setattr(memory_keyring, "get_password", broken)
    monkeypatch.setattr(memory_keyring, "set_password", broken)
    store = SecretStore(tmp_path)
    with pytest.raises(SecretStoreError):
        store.get("k")
    with pytest.raises(SecretStoreError):
        store.put("k2", "v")
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path / "fresh").put("k", "v")  # no store yet: the key is created
    assert issubclass(KeyringLocked, KeyringError)
