"""GET /api/addons and POST /api/addons/refresh: the add-on registry over HTTP."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import gc
import json
import logging
import time
from pathlib import Path

from fakes import FakeSandbox
from test_api import CSRF, harness, signed_in  # noqa: F401  (harness is a fixture)

from hearthkeep.addons import Probes, TailscaleState
from hearthkeep.api.common import RateLimiter
from hearthkeep.sandbox.runner import SandboxStatus
from hearthkeep.secrets import SecretStore
from hearthkeep.startup import StartupStatus

IDS = ["chat", "analysis", "images", "github", "devices", "startup"]
FIELDS = {
    "id",
    "name",
    "about",
    "requires",
    "state",
    "ready",
    "message",
    "action",
    "short",
    "details",
}


class CountingProbes(Probes):
    """Probes that count every check that touches the PC, all ready. (Git and the token are
    read at call time by design, so they aren't counted.)"""

    def __init__(self) -> None:
        self.calls = 0
        self.fresh_sandbox = 0
        super().__init__(
            sandbox=self._sandbox,
            docker_configured_off=False,
            git_available=lambda: True,
            token_saved=lambda: True,
            chat_reachable=self._reachable,
            docker_installed=lambda: self._bump(True),
            tailscale=lambda: self._bump(TailscaleState(True, True, "pc.tail.ts.net")),
            startup=lambda: self._bump(StartupStatus(True, False, None)),
        )

    def _bump(self, value):
        self.calls += 1
        return value

    async def _sandbox(self):
        self.calls += 1
        return SandboxStatus(docker=True, image=True, tag="t")

    async def _reachable(self):
        self.calls += 1
        return True


def test_addons_needs_a_session(harness):
    client = harness.start()
    for response in (client.get("/api/addons"), client.post("/api/addons/refresh", headers=CSRF)):
        assert response.status_code == 401
        assert response.json() == {"reason": "unauthenticated"}
    assert client.post("/api/addons/refresh").status_code == 403  # no X-Hearthkeep: csrf
    assert client.post("/api/addons/refresh").json() == {"reason": "csrf"}


def test_addons_lists_all_six_in_order(harness):
    body = signed_in(harness).get("/api/addons").json()
    assert [a["id"] for a in body["addons"]] == IDS
    assert set(body["addons"][0]) == FIELDS
    assert body["checked_at"] is not None  # start-up awaited one full refresh


def test_first_response_is_checked_not_default(harness):
    """The start-up refresh finishes before serving: analysis isn't the not-ready default and
    an unreachable external chat server is reported from a real probe."""

    async def down():
        return False

    client = signed_in(harness, health=down)
    by_id = {a["id"]: a for a in client.get("/api/addons").json()["addons"]}
    assert by_id["chat"]["state"] == "offline"  # probed, not left at a default
    assert by_id["analysis"]["state"] == "not_set_up"  # Docker configured off, checked


def test_get_is_served_from_cache(harness):
    probes = CountingProbes()
    client = signed_in(harness, addon_probes=probes)
    before = probes.calls
    assert before > 0
    for _ in range(5):
        assert client.get("/api/addons").status_code == 200
    assert probes.calls == before


def test_no_second_pass_right_after_startup(harness):
    probes = CountingProbes()
    signed_in(harness, addon_probes=probes)
    after_start = probes.calls
    time.sleep(0.2)
    assert probes.calls == after_start  # the loop sleeps first


def test_refresh_reruns_checks_and_is_rate_limited(harness):
    probes = CountingProbes()
    client = signed_in(
        harness, addon_probes=probes, limiter=RateLimiter(per_client=2, global_limit=2)
    )
    before = probes.calls
    first = client.post("/api/addons/refresh", headers=CSRF)
    assert first.status_code == 200
    assert [a["id"] for a in first.json()["addons"]] == IDS
    assert probes.calls > before
    assert client.post("/api/addons/refresh", headers=CSRF).status_code == 200
    limited = client.post("/api/addons/refresh", headers=CSRF)
    assert limited.status_code == 429
    assert limited.json() == {"reason": "rate_limited"}


def test_refresh_checks_start_at_login_fresh(harness, monkeypatch):
    from hearthkeep import addons, startup

    calls = []

    def fake_status(*, run=None):
        calls.append(1)
        return StartupStatus(True, False, None)

    monkeypatch.setattr(startup, "status", fake_status)
    monkeypatch.setattr(
        addons, "startup_status", lambda fresh=False: startup.cached_status(0.0, fresh=fresh)
    )
    startup.clear_cache()
    client = signed_in(harness)
    after_start = len(calls)
    assert after_start == 1
    client.post("/api/addons/refresh", headers=CSRF)
    assert len(calls) == after_start + 1  # not served from the 30 s cache


def test_harness_sandbox_means_analysis_ready(harness, tmp_path):
    with_sandbox = signed_in(harness, sandbox=FakeSandbox(tmp_path / "fake"))
    by_id = {a["id"]: a for a in with_sandbox.get("/api/addons").json()["addons"]}
    assert by_id["analysis"]["state"] == "ready" and by_id["analysis"]["ready"] is True


def test_no_sandbox_means_analysis_not_ready(harness):
    by_id = {a["id"]: a for a in signed_in(harness).get("/api/addons").json()["addons"]}
    assert by_id["analysis"]["ready"] is False


def test_addons_json_has_no_paths_or_token(harness):
    from hearthkeep.github.api import TOKEN_SECRET

    client = signed_in(harness)
    SecretStore(Path(harness.data)).put(TOKEN_SECRET, "ghp_secrettokenvalue")
    text = (
        json.dumps(client.get("/api/addons").json())
        + client.post("/api/addons/refresh", headers=CSRF).text
    )
    assert "ghp_secrettokenvalue" not in text
    assert str(harness.data) not in text
    assert str(Path.home()) not in text


def test_background_task_cancelled_on_shutdown(harness, caplog):
    caplog.set_level(logging.DEBUG, logger="asyncio")
    client = signed_in(harness)
    harness.stop(client)
    gc.collect()
    assert "Task was destroyed" not in caplog.text
