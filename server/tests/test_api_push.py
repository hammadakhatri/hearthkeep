"""Push subscriptions over HTTP: one per device (the session's passkey), host-checked."""

# ruff: noqa: F811  (the harness fixture is imported from test_api)

import json
import logging
import mimetypes

from test_api import CSRF, enroll, harness, signed_in, step_up  # noqa: F401  (harness is a fixture)

from hearthkeep.push import VAPID_SECRET
from hearthkeep.secrets import SecretStore

FCM = "https://fcm.googleapis.com/fcm/send/device-one:APA91b-secret"
FCM_2 = "https://fcm.googleapis.com/fcm/send/device-one-new:APA91b-secret"
WNS = "https://db5p.notify.windows.com/w/?token=device-two-secret"


def subscribe(client, endpoint):
    return client.put("/api/push/subscription", json={"endpoint": endpoint}, headers=CSRF)


def add_second_device(harness, client):
    token = step_up(client, "add_device")
    code = client.post("/api/devices/add-code", json={"step_up_token": token}, headers=CSRF)
    enroll(client, code.json()["code"], cred_id="cred-2")  # now signed in as cred-2


def test_push_requires_session(harness):
    client = harness.start()
    assert client.get("/api/push").status_code == 401
    assert subscribe(client, FCM).status_code == 401
    assert client.delete("/api/push/subscription", headers=CSRF).status_code == 401
    assert harness.query("SELECT * FROM push_subscriptions") == []


def test_push_changes_need_csrf(harness):
    client = signed_in(harness)
    response = client.put("/api/push/subscription", json={"endpoint": FCM})
    assert response.status_code == 403
    assert client.delete("/api/push/subscription").status_code == 403


def test_public_key_is_the_stored_vapid_key(harness):
    client = signed_in(harness)
    body = client.get("/api/push").json()
    assert body["enabled"] is False
    assert len(body["public_key"]) == 87  # base64url of 65 bytes, no padding
    pem = SecretStore(harness.data).get(VAPID_SECRET)
    assert pem is not None and pem not in json.dumps(body)


def test_subscribe_binds_to_this_device_and_validates_host(harness):
    client = signed_in(harness)
    refused = subscribe(client, "https://push.example.com/fcm/send/token")
    assert refused.status_code == 422
    assert refused.json() == {"reason": "push_host_not_allowed"}
    assert "push.example.com" not in refused.text
    for bad in (
        "https://fcm.googleapis.com.evil.example/x",
        "https://fcm.googleapis.com./x",
        "http://fcm.googleapis.com/x",
        "https://fcm.googleapis.com/" + "a" * 3000,
        "https://[fcm.googleapis.com/x",
    ):
        assert subscribe(client, bad).status_code == 422
    assert subscribe(client, "").status_code == 422
    assert client.get("/api/push").json()["enabled"] is False

    assert subscribe(client, FCM).status_code == 200
    assert client.get("/api/push").json()["enabled"] is True
    assert subscribe(client, FCM_2).status_code == 200  # the device's new endpoint replaces it
    assert harness.query("SELECT credential_id, endpoint FROM push_subscriptions") == [
        ("cred-1", FCM_2)
    ]
    assert client.get("/api/push").json()["endpoint"] == FCM_2  # lets the page spot drift

    add_second_device(harness, client)
    state = client.get("/api/push").json()
    assert state["enabled"] is False and state["endpoint"] is None  # this is another device
    assert subscribe(client, WNS).status_code == 200
    assert sorted(harness.query("SELECT credential_id, endpoint FROM push_subscriptions")) == [
        ("cred-1", FCM_2),
        ("cred-2", WNS),
    ]

    events = [e for e in reversed(harness.events()) if e["event"].startswith("push.")]
    assert [(e["event"], e["detail"]) for e in events] == [
        ("push.enabled", {"credential_id": "cred-1"}),
        ("push.enabled", {"credential_id": "cred-1"}),
        ("push.enabled", {"credential_id": "cred-2"}),
    ]
    everything = json.dumps(harness.events())
    assert "secret" not in everything and "fcm.googleapis.com" not in everything


def test_unsubscribe(harness):
    client = signed_in(harness)
    assert subscribe(client, FCM).status_code == 200
    response = client.delete("/api/push/subscription", headers=CSRF)
    assert response.status_code == 200
    assert client.get("/api/push").json()["enabled"] is False
    assert harness.query("SELECT * FROM push_subscriptions") == []
    assert client.delete("/api/push/subscription", headers=CSRF).status_code == 200  # idempotent
    disabled = [e for e in harness.events() if e["event"] == "push.disabled"]
    assert [e["detail"] for e in disabled] == [{"credential_id": "cred-1"}]


def test_unsubscribe_touches_only_this_device(harness):
    client = signed_in(harness)
    subscribe(client, FCM)
    add_second_device(harness, client)
    subscribe(client, WNS)
    client.delete("/api/push/subscription", headers=CSRF)
    assert harness.query("SELECT credential_id FROM push_subscriptions") == [("cred-1",)]


def test_removing_a_device_deletes_its_subscription(harness):
    client = signed_in(harness)
    subscribe(client, FCM)
    add_second_device(harness, client)
    subscribe(client, WNS)

    assert client.delete("/api/devices/cred-1", headers=CSRF).status_code == 200
    assert harness.query("SELECT credential_id, endpoint FROM push_subscriptions") == [
        ("cred-2", WNS)
    ]
    disabled = [e for e in harness.events() if e["event"] == "push.disabled"]
    assert [e["detail"] for e in disabled] == [
        {"credential_id": "cred-1", "reason": "credential_revoked"}
    ]


def test_service_worker_is_served_as_script_under_the_csp(harness, tmp_path):
    """`/sw.js` registers under the existing CSP: same origin, a JavaScript type, and
    `default-src 'self'` covers worker-src and script-src."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('push', () => {})", encoding="utf-8")
    client = harness.start(web_dist=dist)
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "worker-src" not in csp and "script-src" not in csp and "child-src" not in csp


def test_js_is_served_as_javascript_even_if_the_registry_says_otherwise(harness, tmp_path):
    """Windows can map .js to text/plain in the registry; a service worker then won't load."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "sw.js").write_text("self.addEventListener('push', () => {})", encoding="utf-8")
    (dist / "mod.mjs").write_text("export {}", encoding="utf-8")
    mimetypes.add_type("text/plain", ".js")
    mimetypes.add_type("text/plain", ".mjs")
    try:
        client = harness.start(web_dist=dist)
        assert client.get("/sw.js").headers["content-type"].startswith("text/javascript")
        assert client.get("/mod.mjs").headers["content-type"].startswith("text/javascript")
    finally:
        mimetypes.add_type("text/javascript", ".js")
        mimetypes.add_type("text/javascript", ".mjs")


def test_push_off_but_hearthkeep_starts_when_the_keyring_fails(harness, memory_keyring,
                                                               monkeypatch, caplog):  # fmt: skip
    from keyring.errors import KeyringLocked

    def broken(*args):
        raise KeyringLocked("the keyring is locked")

    monkeypatch.setattr(memory_keyring, "get_password", broken)
    monkeypatch.setattr(memory_keyring, "set_password", broken)
    caplog.set_level(logging.WARNING)
    client = signed_in(harness)
    assert client.get("/api/push").json() == {"reason": "push_unavailable"}
    assert client.get("/api/push").status_code == 503
    assert subscribe(client, FCM).status_code == 503
    assert client.delete("/api/push/subscription", headers=CSRF).status_code == 503
    assert "SecretStoreError" in caplog.text


def test_push_off_but_hearthkeep_starts_with_a_corrupt_vapid_key(harness, caplog):
    garbage = "not-a-pem-but-maybe-secret-material-0123456789"
    SecretStore(harness.data).put(VAPID_SECRET, garbage)
    caplog.set_level(logging.DEBUG)
    client = signed_in(harness)
    response = client.get("/api/push")
    assert response.status_code == 503 and response.json() == {"reason": "push_unavailable"}
    assert garbage not in caplog.text
    assert "ValueError" in caplog.text
    assert SecretStore(harness.data).get(VAPID_SECRET) == garbage  # never overwritten
