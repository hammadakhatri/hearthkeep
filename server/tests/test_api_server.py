"""Stop and restart from the app: the server info, the `server` step-up, the audit trail and
`setup_mode` in /api/health."""
# ruff: noqa: F811  (the harness fixture is imported from test_api)

import pytest
from test_api import (  # noqa: F401  (harness is a fixture)
    CSRF,
    harness,
    login,
    signed_in,
    step_up,
)

import hearthkeep
from hearthkeep.lifecycle import EXIT_RESTART, EXIT_STOPPED, ServerControl


def other_session_token(client) -> str:
    """A valid `server` step-up token bound to a second session signed into the same app
    (a sign-in that presents no cookie leaves the first session alive). The client is left
    on its first session."""
    first = next(c for c in client.cookies.jar if c.name == "hk_session")
    client.cookies.clear()
    assert login(client).status_code == 200
    second = client.cookies.get("hk_session")
    assert second and second != first.value
    token = step_up(client, "server")
    client.cookies.clear()
    client.cookies.jar.set_cookie(first)
    assert client.get("/api/server").status_code == 200
    return token


def test_server_routes_need_a_session(harness):
    client = harness.start()
    assert client.get("/api/server").status_code == 401
    for action in ("restart", "stop"):
        body = {"step_up_token": "x"}
        assert client.post(f"/api/server/{action}", json=body, headers=CSRF).status_code == 401
        no_header = client.post(f"/api/server/{action}", json=body)
        assert no_header.status_code == 403 and no_header.json() == {"reason": "csrf"}


def test_server_info(harness):
    control = ServerControl(supervised=True)
    control.mark_restart_needed("chat_model")
    body = signed_in(harness, control=control).get("/api/server").json()
    assert body["started_at"] == control.started_at.isoformat()
    assert body | {"started_at": None} == {
        "version": hearthkeep.__version__,
        "supervised": True,
        "setup_mode": False,
        "started_at": None,
        "restart_needed": True,
        "restart_reasons": ["chat_model"],
    }


def test_server_info_when_nothing_is_pending(harness):
    body = signed_in(harness).get("/api/server").json()
    assert body["supervised"] is False and body["restart_needed"] is False
    assert body["restart_reasons"] == []


@pytest.mark.parametrize(("action", "code"), [("restart", EXIT_RESTART), ("stop", EXIT_STOPPED)])
def test_restart_and_stop_need_step_up(harness, action, code):
    control = ServerControl(supervised=True)
    shutdowns = []
    control.attach(lambda: shutdowns.append(1))
    client = signed_in(harness, control=control)
    used = step_up(client, "server")
    wrong_purpose = step_up(client, "settings")
    other_session = other_session_token(client)
    path = f"/api/server/{action}"
    # Everything that must be refused is tried first: nothing may ask the server to stop.
    for body in (
        {},
        {"step_up_token": "nope"},
        {"step_up_token": wrong_purpose},
        {"step_up_token": other_session},
    ):
        r = client.post(path, json=body, headers=CSRF)
        assert r.status_code == 403 and r.json() == {"reason": "step_up_required"}, body
        assert control.exit_code is None and shutdowns == []
    ok = client.post(path, json={"step_up_token": used}, headers=CSRF)
    assert ok.status_code == 202 and ok.json() == {"ok": True}
    assert control.exit_code == code and shutdowns == [1]
    # A token is good once: replaying it on a server that came back is refused.
    fresh = ServerControl(supervised=True)
    fresh_shutdowns = []
    fresh.attach(lambda: fresh_shutdowns.append(1))
    client.app.state.hk.control = fresh
    reused = client.post(path, json={"step_up_token": used}, headers=CSRF)
    assert reused.status_code == 403 and fresh.exit_code is None and fresh_shutdowns == []
    events = [e["event"] for e in harness.events()]
    assert events.count(f"server.{action}_denied") == 5
    assert events.count(f"server.{action}") == 1


def test_denied_rows_carry_only_the_reason(harness):
    control = ServerControl(supervised=True)
    client = signed_in(harness, control=control)
    client.post("/api/server/stop", json={}, headers=CSRF)
    client.post("/api/server/restart", json={"step_up_token": "nope"}, headers=CSRF)
    rows = {e["event"]: e["detail"] for e in harness.events() if e["event"].startswith("server.")}
    assert rows == {
        "server.stop_denied": {"reason": "step_up_required"},
        "server.restart_denied": {"reason": "step_up_required"},
    }


def test_restart_unsupervised_is_409_and_keeps_the_token(harness):
    control = ServerControl(supervised=False)
    client = signed_in(harness, control=control)
    token = step_up(client, "server")
    refused = client.post("/api/server/restart", json={"step_up_token": token}, headers=CSRF)
    assert refused.status_code == 409 and refused.json()["reason"] == "not_supervised"
    assert control.exit_code is None
    assert not [e for e in harness.events() if e["event"].startswith("server.restart")]
    # The refusal came before the token was looked at, so it still works for a stop.
    stopped = client.post("/api/server/stop", json={"step_up_token": token}, headers=CSRF)
    assert stopped.status_code == 202 and control.exit_code == EXIT_STOPPED


def test_restart_with_step_up_answers_then_exits(harness):
    control = ServerControl(supervised=True)
    control.mark_restart_needed("chat_model")
    seen = []
    control.attach(lambda: seen.append(control.exit_code))
    client = signed_in(harness, control=control)
    token = step_up(client, "server")
    response = client.post("/api/server/restart", json={"step_up_token": token}, headers=CSRF)
    assert response.status_code == 202 and response.json() == {"ok": True}
    assert control.exit_code == EXIT_RESTART and seen == [EXIT_RESTART]
    row = next(e for e in harness.events() if e["event"] == "server.restart")
    assert row["detail"] == {"reasons": ["chat_model"]}


def test_health_reports_setup_mode(harness):
    assert harness.start().get("/api/health").json()["setup_mode"] is False
    on = harness.start(control=ServerControl(supervised=False, setup_mode=True))
    assert on.get("/api/health").json()["setup_mode"] is True
