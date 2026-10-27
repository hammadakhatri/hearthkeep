"""Payload-free web push: the endpoint allow-list, VAPID signing, the sender and the
per-device subscriptions."""

import base64
import json
import logging

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from hearthkeep import push
from hearthkeep.db import connect
from hearthkeep.push import (
    VAPID_SECRET,
    PushEndpointError,
    PushSender,
    PushSubscriptions,
    VapidKeys,
    check_endpoint,
)
from hearthkeep.secrets import SecretStore

NOW = 1_800_000_000
FCM = "https://fcm.googleapis.com/fcm/send/dEviCe-ToKeN:APA91bH-secret-part"
WNS = "https://db5p.notify.windows.com/w/?token=BQYAAAB-secret-part"


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---- the allow-list ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        FCM,
        WNS,
        "https://updates.push.services.mozilla.com/wpush/v2/gAAAAAB-token",
        "https://web.push.apple.com/QGuQyavXutnMH-token",
        "https://FCM.GOOGLEAPIS.COM/fcm/send/token",  # host names compare lowercased
        "https://fcm.googleapis.com:443/fcm/send/token",
    ],
)
def test_check_endpoint_allows_push_services(url):
    assert check_endpoint(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://fcm.googleapis.com/fcm/send/token",  # not https
        "https://push.example.com/fcm/send/token",  # another host
        "https://user:pass@fcm.googleapis.com/fcm/send/token",  # userinfo
        "https://user@fcm.googleapis.com/fcm/send/token",
        "https://fcm.googleapis.com:8443/fcm/send/token",  # another port
        "https://fcm.googleapis.com:/fcm/send/token",  # an empty port
        "https://fcm.googleapis.com.evil.example/fcm/send/token",
        "https://evil.notify.windows.com.attacker.example/w/?token=x",
        "https://fcm.googleapis.com./fcm/send/token",  # trailing dot
        "https://notify.windows.com/w/?token=x",  # the suffix itself is no host
        "https://a.b.notify.windows.com/w/?token=x",  # exactly one label before the suffix
        "https://.notify.windows.com/w/?token=x",
        "https://-db5p.notify.windows.com/w/?token=x",
        "https://xn--fcm-googleapis-com.example/fcm/send/token",  # punycode lookalikes
        "https://fcm.googleapis.xn--com-9ka/fcm/send/token",
        "https://xn--db5p-abc.notify.windows.com/w/?token=x",
        "https://fcm.googleapis.cοm/fcm/send/token",  # Greek omicron
        "https://ｆｃｍ.googleapis.com/fcm/send/token",  # fullwidth "fcm"
        "https://142.250.180.10/fcm/send/token",  # IP literals
        "https://[2a00:1450:4009::5f]/fcm/send/token",
        "https://fcm.googleapis.com/fcm/send/token#fragment",
        "https://fcm.googleapis.com/fcm/send/" + "a" * 1791,  # path over 1,800
        "https://db5p.notify.windows.com/w/?token=" + "a" * 2048,  # over 2,048 in all
        "https://fcm.googleapis.com\\@evil.example/fcm/send/token",
        "https://fcm.googleapis.com/fcm/send/to ken",
        "https://fcm.googleapis.com/fcm/send/to\nken",
        "fcm.googleapis.com/fcm/send/token",
        "https://[fcm.googleapis.com/fcm/send/token",  # urlsplit itself refuses these
        "https://fcm.googleapis.com]/fcm/send/token",
        "",
    ],
)
def test_check_endpoint_refuses_everything_else(url):
    with pytest.raises(PushEndpointError):
        check_endpoint(url)


def test_check_endpoint_allow_list():
    """The brief's named cases, in one place."""
    assert check_endpoint(FCM) == FCM
    assert check_endpoint(WNS) == WNS
    for bad in (
        "http://fcm.googleapis.com/fcm/send/x",
        "https://example.com/fcm/send/x",
        "https://u:p@fcm.googleapis.com/fcm/send/x",
        "https://fcm.googleapis.com:8443/fcm/send/x",
        "https://fcm.googleapis.com.evil.example/fcm/send/x",
    ):
        with pytest.raises(PushEndpointError):
            check_endpoint(bad)


def test_path_of_exactly_1800_is_allowed():
    path = "/fcm/send/" + "a" * (1800 - len("/fcm/send/"))
    assert check_endpoint("https://fcm.googleapis.com" + path)


def test_endpoint_error_never_carries_the_url():
    with pytest.raises(PushEndpointError) as exc:
        check_endpoint("https://push.example.com/secret-token-path")
    assert "secret-token-path" not in str(exc.value)


# ---- VAPID ------------------------------------------------------------------------------


def test_vapid_keys_created_once_in_secret_store(tmp_path):
    store = SecretStore(tmp_path)
    assert store.get(VAPID_SECRET) is None
    first = VapidKeys.load_or_create(store)
    pem = store.get(VAPID_SECRET)
    assert pem is not None
    stored = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    assert isinstance(stored, ec.EllipticCurvePrivateKey) and stored.curve.name == "secp256r1"
    second = VapidKeys.load_or_create(SecretStore(tmp_path))
    assert second.public_key == first.public_key
    assert store.get(VAPID_SECRET) == pem
    assert store.names() == [VAPID_SECRET]

    point = b64url_decode(first.public_key)
    assert len(point) == 65 and point[0] == 0x04
    assert "=" not in first.public_key and "+" not in first.public_key


def test_vapid_jwt_verifies_with_public_key(tmp_path):
    keys = VapidKeys.load_or_create(SecretStore(tmp_path))
    value = keys.authorization("https://fcm.googleapis.com:443/fcm/send/token", now=NOW)
    assert value.startswith("vapid t=")
    token, k = value.removeprefix("vapid t=").split(", k=")
    assert k == keys.public_key

    head, claims, signature = token.split(".")
    assert json.loads(b64url_decode(head)) == {"typ": "JWT", "alg": "ES256"}
    body = json.loads(b64url_decode(claims))
    assert body == {
        "aud": "https://fcm.googleapis.com",
        "exp": NOW + 43200,
        "sub": "mailto:owner@hearthkeep.invalid",
    }
    assert NOW < body["exp"] <= NOW + 24 * 3600

    raw = b64url_decode(signature)
    assert len(raw) == 64  # raw r||s, not DER
    r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(k))
    public.verify(
        encode_dss_signature(r, s), f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())
    )
    with pytest.raises(InvalidSignature):
        public.verify(
            encode_dss_signature(r, s), f"{head}.{claims}x".encode(), ec.ECDSA(hashes.SHA256())
        )


def test_vapid_audience_is_each_endpoints_origin(tmp_path):
    keys = VapidKeys.load_or_create(SecretStore(tmp_path))
    token = keys.authorization(WNS, now=NOW).removeprefix("vapid t=").split(", k=")[0]
    assert json.loads(b64url_decode(token.split(".")[1]))["aud"] == (
        "https://db5p.notify.windows.com"
    )


# ---- the sender -------------------------------------------------------------------------


@pytest.fixture
def keys(tmp_path):
    return VapidKeys.load_or_create(SecretStore(tmp_path))


class Recorder:
    def __init__(self, status=201, headers=None):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.headers = headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, headers=self.headers)


async def sender_with(keys, recorder):
    return PushSender(keys, transport=httpx.MockTransport(recorder), clock=lambda: NOW)


async def test_notify_sends_empty_body_with_headers(keys):
    recorder = Recorder(201)
    sender = await sender_with(keys, recorder)
    try:
        assert await sender.notify(FCM) == "sent"
    finally:
        await sender.aclose()
    [request] = recorder.requests
    assert request.method == "POST"
    assert str(request.url) == FCM
    assert request.content == b""
    assert request.headers["TTL"] == "86400"
    assert request.headers["Urgency"] == "normal"
    assert request.headers["Content-Length"] == "0"
    assert request.headers["Authorization"].startswith("vapid t=")
    # Same signed claims and key as signing for this endpoint now (ECDSA signatures vary).
    expected = keys.authorization(FCM, now=NOW)
    sent = request.headers["Authorization"]
    assert sent.rsplit(".", 1)[0] == expected.rsplit(".", 1)[0]
    assert sent.split(", k=")[1] == keys.public_key
    assert request.headers["User-Agent"] == "Hearthkeep"
    assert "Content-Encoding" not in request.headers
    assert "Content-Type" not in request.headers
    assert "Cookie" not in request.headers
    assert "Crypto-Key" not in request.headers and "Encryption" not in request.headers


@pytest.mark.parametrize("status", [200, 201, 202])
async def test_notify_sent_on_2xx(keys, status):
    sender = await sender_with(keys, Recorder(status))
    assert await sender.notify(FCM) == "sent"
    await sender.aclose()


@pytest.mark.parametrize("status", [404, 410])
async def test_notify_gone_on_404_and_410(keys, status):
    sender = await sender_with(keys, Recorder(status))
    assert await sender.notify(FCM) == "gone"
    await sender.aclose()


@pytest.mark.parametrize("status", [204, 400, 401, 403, 413, 429, 500, 503])
async def test_notify_failed_on_anything_else(keys, status):
    sender = await sender_with(keys, Recorder(status))
    assert await sender.notify(FCM) == "failed"
    await sender.aclose()


async def test_notify_never_follows_redirects(keys):
    recorder = Recorder(302, headers={"Location": "https://fcm.googleapis.com/elsewhere"})
    sender = await sender_with(keys, recorder)
    assert await sender.notify(FCM) == "failed"
    await sender.aclose()
    assert len(recorder.requests) == 1


async def test_notify_has_an_overall_time_limit(keys, monkeypatch):
    """A service that trickles its answer can't hold a send past the overall limit."""
    import asyncio

    monkeypatch.setattr(push, "PUSH_TIMEOUT_SECONDS", 0.05)

    async def stalls(request):
        await asyncio.sleep(30)
        return httpx.Response(201)

    sender = await sender_with(keys, stalls)
    started = asyncio.get_running_loop().time()
    assert await sender.notify(FCM) == "failed"
    assert asyncio.get_running_loop().time() - started < 5
    await sender.aclose()


async def test_notify_failed_on_network_error(keys):
    def broken(request):
        raise httpx.ConnectError("no route to " + str(request.url))

    sender = await sender_with(keys, broken)
    assert await sender.notify(FCM) == "failed"
    await sender.aclose()


async def test_disallowed_endpoint_is_never_requested(keys):
    recorder = Recorder(201)
    sender = await sender_with(keys, recorder)
    for url in (
        "https://push.example.com/fcm/send/token",
        "http://fcm.googleapis.com/fcm/send/token",
        "https://fcm.googleapis.com.evil.example/fcm/send/token",
        "https://127.0.0.1/fcm/send/token",
    ):
        assert await sender.notify(url) == "failed"
    await sender.aclose()
    assert recorder.requests == []


async def test_notify_never_sends_cookies(keys):
    recorder = Recorder(201, headers={"Set-Cookie": "track=1; Path=/; Secure"})
    sender = await sender_with(keys, recorder)
    assert await sender.notify(FCM) == "sent"
    assert await sender.notify(FCM) == "sent"
    await sender.aclose()
    assert [r.headers.get("Cookie") for r in recorder.requests] == [None, None]


async def test_one_client_per_sender_and_it_closes(keys):
    recorder = Recorder(201)
    sender = await sender_with(keys, recorder)
    client = sender._client
    await sender.notify(FCM)
    await sender.notify(WNS)
    assert sender._client is client
    await sender.aclose()
    assert client.is_closed


async def test_endpoint_url_is_never_logged(keys, caplog):
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(201)
    sender = await sender_with(keys, recorder)
    await sender.notify(FCM)
    await sender.notify("https://push.example.com/refused-secret-path")
    sender_gone = await sender_with(keys, Recorder(410))
    await sender_gone.notify(WNS)
    await sender.aclose()
    await sender_gone.aclose()
    text = caplog.text
    assert "secret-part" not in text and "dEviCe-ToKeN" not in text
    assert "refused-secret-path" not in text
    assert "fcm.googleapis.com" in text and "201" in text
    assert "db5p.notify.windows.com" in text and "410" in text


def test_sender_client_settings(keys):
    sender = PushSender(keys)
    client = sender._client
    assert client.trust_env is False
    assert client.follow_redirects is False
    assert client.timeout == httpx.Timeout(10)
    assert client.headers["User-Agent"] == "Hearthkeep"


# ---- subscriptions ----------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "hk.db")
    yield c
    c.close()


def test_subscriptions_one_per_credential(conn):
    subs = PushSubscriptions(conn)
    assert subs.endpoint_for("cred-1") is None
    subs.set("cred-1", FCM)
    subs.set("cred-2", WNS)
    assert subs.endpoint_for("cred-1") == FCM
    replacement = FCM.replace("dEviCe", "nEw")
    subs.set("cred-1", replacement)  # replaces, never adds
    assert subs.endpoint_for("cred-1") == replacement
    assert conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] == 2

    assert subs.remove("cred-1") is True
    assert subs.remove("cred-1") is False
    assert subs.endpoint_for("cred-1") is None

    subs.remove_endpoint(WNS)
    assert subs.endpoint_for("cred-2") is None


def test_subscriptions_refuse_disallowed_endpoints(conn):
    subs = PushSubscriptions(conn)
    with pytest.raises(PushEndpointError):
        subs.set("cred-1", "https://push.example.com/x")
    assert subs.endpoint_for("cred-1") is None


async def test_stored_endpoint_no_longer_allowed_is_never_requested(conn, keys):
    """A row saved before an allow-list change (inserted directly here) is checked again
    before every send and never requested."""
    conn.execute(
        "INSERT INTO push_subscriptions (credential_id, endpoint, created_at)"
        " VALUES ('cred-1', 'https://push.retired.example/fcm/send/token', '2026-01-01')"
    )
    recorder = Recorder(201)
    sender = await sender_with(keys, recorder)
    endpoint = PushSubscriptions(conn).endpoint_for("cred-1")
    assert endpoint is not None
    assert await sender.notify(endpoint) == "failed"
    await sender.aclose()
    assert recorder.requests == []


def test_module_has_no_payload_encryption():
    """Payload-free by design: no aes128gcm, no Content-Encoding anywhere in the sender."""
    source = open(push.__file__, encoding="utf-8").read()
    assert "aes128gcm" not in source and "Content-Encoding" not in source
