import subprocess
from pathlib import Path

import pytest

from hearthkeep import config
from hearthkeep.config import ConfigError, detect_docker, init_data_dir, load_settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def init(path: Path) -> Path:
    init_data_dir(path)
    return path


def write(path: Path, body: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.toml").write_text(body + "\n", encoding="utf-8")
    return path


def test_defaults_when_config_minimal(tmp_path):
    s = load_settings(init(tmp_path))
    assert s.host == "127.0.0.1" and s.port == 8765
    assert s.data_dir == tmp_path


def test_non_loopback_host_rejected(tmp_path):
    write(tmp_path, 'host = "0.0.0.0"')
    with pytest.raises(ConfigError, match="Tailscale"):
        load_settings(tmp_path)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_accepted(tmp_path, host):
    write(tmp_path, f'host = "{host}"')
    assert load_settings(tmp_path).host == host


def test_public_origin_must_be_https(tmp_path):
    write(tmp_path, 'public_origin = "http://myhost.example.com"')
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


def test_localhost_origin_may_be_http(tmp_path):
    write(tmp_path, 'public_origin = "http://localhost:8765"')
    load_settings(tmp_path)


def test_rp_id_must_match_origin_host(tmp_path):
    write(tmp_path, 'public_origin = "https://box.tail1234.ts.net"\nrp_id = "other.ts.net"')
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


def test_https_origin_with_matching_rp_id(tmp_path):
    write(tmp_path, 'public_origin = "https://box.tail1234.ts.net"\nrp_id = "box.tail1234.ts.net"')
    assert load_settings(tmp_path).rp_id == "box.tail1234.ts.net"


def test_unknown_key_rejected_by_name(tmp_path):
    write(tmp_path, 'hots = "127.0.0.1"')
    with pytest.raises(ConfigError, match="hots"):
        load_settings(tmp_path)


def test_data_dir_key_not_settable(tmp_path):
    write(tmp_path, 'data_dir = "/elsewhere"')
    with pytest.raises(ConfigError, match="data_dir"):
        load_settings(tmp_path)


def test_wrong_type_rejected(tmp_path):
    write(tmp_path, 'port = "8765"')
    with pytest.raises(ConfigError, match="port"):
        load_settings(tmp_path)


def test_invalid_toml_rejected(tmp_path):
    write(tmp_path, "host = ")
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


def test_missing_config_uses_defaults(tmp_path):
    assert load_settings(tmp_path).port == 8765


def test_init_creates_structure(tmp_path):
    init_data_dir(tmp_path)
    assert (tmp_path / "workspace" / "output").is_dir()
    assert (tmp_path / "workspace" / "repos").is_dir()
    assert (tmp_path / "workspace" / "output" / "images").is_dir()
    assert (tmp_path / "workspace" / "uploads").is_dir()
    for name in ("secrets", "logs", "private"):
        assert (tmp_path / name).is_dir()
    assert (tmp_path / "config.toml").exists()


def test_init_does_not_overwrite_config(tmp_path):
    write(tmp_path, "port = 9000")
    init_data_dir(tmp_path)
    assert load_settings(tmp_path).port == 9000


def test_default_config_loads(tmp_path):
    assert load_settings(init(tmp_path)).chat_model == "gemma-4-12b"


def test_example_copies_identical():
    root = (REPO_ROOT / "config.example.toml").read_bytes()
    packaged = (Path(config.__file__).parent / "config.example.toml").read_bytes()
    assert root == packaged


def test_detect_docker_true(monkeypatch):
    def fake_run(cmd, **kw):
        assert cmd == ["docker", "info"] and kw["timeout"] == 5
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    assert detect_docker() is True


def test_detect_docker_nonzero(monkeypatch):
    monkeypatch.setattr(
        config.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )
    assert detect_docker() is False


@pytest.mark.parametrize(
    "exc", [FileNotFoundError(), subprocess.TimeoutExpired("docker", 5), OSError("boom")]
)
def test_detect_docker_never_raises(monkeypatch, exc):
    def fake_run(cmd, **kw):
        raise exc

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    assert detect_docker() is False


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8080/v1", "http://localhost:1234/v1", "http://[::1]:8080/v1"]
)
def test_loopback_chat_base_url_accepted(tmp_path, url):
    write(tmp_path, f'chat_base_url = "{url}"')
    assert load_settings(tmp_path).chat_base_url == url


@pytest.mark.parametrize(
    "url", ["https://api.example.com/v1", "http://192.168.1.5:8080/v1", "ftp://localhost/v1"]
)
def test_remote_chat_base_url_rejected(tmp_path, url):
    write(tmp_path, f'chat_base_url = "{url}"')
    with pytest.raises(ConfigError, match="local models"):
        load_settings(tmp_path)


@pytest.mark.parametrize(
    "line",
    [
        "port = 0",
        "port = 65536",
        "session_idle_days = 0",
        "approval_ttl_minutes = 0",
        "max_agent_steps = 0",
        "max_agent_steps = 51",
    ],
)
def test_out_of_range_rejected(tmp_path, line):
    write(tmp_path, line)
    with pytest.raises(ConfigError, match=line.split()[0]):
        load_settings(tmp_path)


def test_range_boundaries_accepted(tmp_path):
    write(
        tmp_path,
        "port = 65535\nsession_idle_days = 1\napproval_ttl_minutes = 1\nmax_agent_steps = 50",
    )
    assert load_settings(tmp_path).max_agent_steps == 50


def test_detect_docker_never_flashes_a_window_on_windows(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(config.subprocess, "run", fake_run)
    monkeypatch.setattr(config.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    detect_docker()
    assert seen["creationflags"] == 0x08000000
