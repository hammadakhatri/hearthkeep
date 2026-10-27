"""Payload-free web push to the owner's devices.

Hearthkeep's only outgoing connection of its own: one HTTPS POST with an empty body to the
push service a browser gave us. Nothing is encrypted because nothing is sent; the device's
service worker shows a fixed notice. Endpoints are checked against an allow-list when a device
subscribes and again before every send. The endpoint URL (it carries the device's token) is
never logged or audited: only its host and the status code are logged.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Literal
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from hearthkeep import db
from hearthkeep.secrets import SecretStore

PUSH_HOSTS = frozenset(
    {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
)
PUSH_HOST_SUFFIXES = (".notify.windows.com",)
MAX_ENDPOINT = 2048
MAX_PATH = 1800

VAPID_SECRET = "vapid_private_key"
VAPID_SUBJECT = "mailto:owner@hearthkeep.invalid"
VAPID_LIFETIME_SECONDS = 43200
PUSH_TTL_SECONDS = 86400
PUSH_TIMEOUT_SECONDS = 10
USER_AGENT = "Hearthkeep"

PushOutcome = Literal["sent", "gone", "failed"]

# One DNS label in front of a suffix: letters, digits and inner hyphens, at most 63, and never
# an IDNA A-label ("xn--"), so no lookalike can stand in for a push service's own name.
_LABEL = re.compile(r"(?!xn--)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# Printable ASCII without space or backslash: what every push service's endpoint is made of.
_URL_CHARS = re.compile(r"[!-\[\]-~]+")

_log = logging.getLogger(__name__)


class PushEndpointError(Exception):
    """The endpoint is not an allowed push service. The message never contains the URL."""


def _host_allowed(host: str) -> bool:
    if host in PUSH_HOSTS:
        return True
    for suffix in PUSH_HOST_SUFFIXES:
        if host.endswith(suffix) and _LABEL.fullmatch(host.removesuffix(suffix)):
            return True
    return False


def check_endpoint(url: str) -> str:
    """The endpoint unchanged if it is an https URL on an allowed push service, else
    PushEndpointError. Host names compare in lowercase ASCII: any non-ASCII host (whose IDNA
    form could never equal an allowed name) is refused rather than normalized, so a Unicode
    lookalike can't fold into one. IP literals, a trailing dot, userinfo, a port other than
    443, a fragment and a path over 1,800 characters are refused."""
    if not isinstance(url, str) or not 0 < len(url) <= MAX_ENDPOINT:
        raise PushEndpointError("endpoint missing or too long")
    if not _URL_CHARS.fullmatch(url):
        raise PushEndpointError("endpoint has characters no push service uses")
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. unbalanced IPv6 brackets
        raise PushEndpointError("endpoint is not a URL") from None
    if parts.scheme != "https" or not url.startswith("https://"):
        raise PushEndpointError("endpoint is not https")
    if "#" in url:
        raise PushEndpointError("endpoint has a fragment")
    if "@" in parts.netloc:
        raise PushEndpointError("endpoint has userinfo")
    host, colon, port = parts.netloc.partition(":")
    if colon and port != "443":
        raise PushEndpointError("endpoint port is not 443")
    host = host.lower()
    if not host or host.endswith(".") or host.startswith("["):
        raise PushEndpointError("endpoint host is not a push service")
    if not _host_allowed(host):  # also refuses every IP literal: none is on the list
        raise PushEndpointError("endpoint host is not a push service")
    if len(parts.path) > MAX_PATH:
        raise PushEndpointError("endpoint path too long")
    return url


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class VapidKeys:
    """The server's P-256 VAPID key, kept as PEM in the secret store and created once."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey) -> None:
        self._key = private_key
        point = private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.public_key = _b64url(point)  # what browsers take as applicationServerKey

    @classmethod
    def load_or_create(cls, store: SecretStore) -> VapidKeys:
        pem = store.get(VAPID_SECRET)
        if pem is None:
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode("ascii")
            store.put(VAPID_SECRET, pem)
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("the stored VAPID key is not a P-256 key")
        return cls(key)

    def authorization(self, endpoint: str, now: int) -> str:
        """The `Authorization` header for one push: `vapid t=<ES256 JWT>, k=<public key>`."""
        parts = urlsplit(endpoint)
        claims = {
            "aud": f"{parts.scheme}://{parts.hostname}",
            "exp": int(now) + VAPID_LIFETIME_SECONDS,
            "sub": VAPID_SUBJECT,
        }
        header = {"typ": "JWT", "alg": "ES256"}
        signing_input = ".".join(
            _b64url(json.dumps(part, separators=(",", ":")).encode()) for part in (header, claims)
        )
        der = self._key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"vapid t={signing_input}.{_b64url(signature)}, k={self.public_key}"


class _NoCookies(DefaultCookiePolicy):
    """Never store a cookie a push service sets, never send one."""

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False


class _HideEndpoints(logging.Filter):
    """httpx logs each request's full URL at INFO; for a push service only the host is kept,
    since the rest of a push endpoint is the device's token."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and any(
            isinstance(a, httpx.URL) and _host_allowed(a.host) for a in record.args
        ):
            record.args = tuple(
                f"{a.scheme}://{a.host}" if isinstance(a, httpx.URL) else a for a in record.args
            )
        return True


logging.getLogger("httpx").addFilter(_HideEndpoints())


class PushSender:
    """Sends payload-free pushes over one shared client; `aclose` it at shutdown."""

    def __init__(
        self,
        keys: VapidKeys,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._keys = keys
        self._clock = clock
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=PUSH_TIMEOUT_SECONDS,
            transport=transport,
            headers={"User-Agent": USER_AGENT},
            cookies=CookieJar(policy=_NoCookies()),
        )

    @property
    def public_key(self) -> str:
        return self._keys.public_key

    async def notify(self, endpoint: str) -> PushOutcome:
        """One POST with an empty body. 200/201/202 → sent, 404/410 → gone (the device
        unsubscribed), anything else, a refused endpoint or a network error → failed."""
        try:
            url = check_endpoint(endpoint)
        except PushEndpointError:
            _log.warning("push not sent: endpoint not allowed")
            return "failed"
        host = urlsplit(url).hostname
        headers = {
            "TTL": str(PUSH_TTL_SECONDS),
            "Urgency": "normal",
            "Authorization": self._keys.authorization(url, int(self._clock())),
            "Content-Length": "0",
        }
        try:
            async with self._client.stream("POST", url, content=b"", headers=headers) as response:
                status = response.status_code
        except Exception as exc:
            # Only the type: an httpx error's text carries the URL.
            _log.warning("push to %s failed: %s", host, type(exc).__name__)
            return "failed"
        finally:
            self._client.cookies.clear()
        _log.info("push to %s: %d", host, status)
        if status in (200, 201, 202):
            return "sent"
        if status in (404, 410):
            return "gone"
        return "failed"

    async def aclose(self) -> None:
        await self._client.aclose()


class PushSubscriptions:
    """One push endpoint per device, keyed by the device's passkey (credential) id."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def set(self, credential_id: str, endpoint: str) -> None:
        """Save this device's endpoint, replacing its previous one. Refuses a disallowed
        endpoint with PushEndpointError."""
        check_endpoint(endpoint)
        self._conn.execute(
            "INSERT INTO push_subscriptions (credential_id, endpoint, created_at)"
            " VALUES (?, ?, ?) ON CONFLICT (credential_id)"
            " DO UPDATE SET endpoint = excluded.endpoint, created_at = excluded.created_at",
            (credential_id, endpoint, db.utcnow()),
        )

    def remove(self, credential_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM push_subscriptions WHERE credential_id = ?", (credential_id,)
        )
        return cursor.rowcount > 0

    def endpoint_for(self, credential_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT endpoint FROM push_subscriptions WHERE credential_id = ?", (credential_id,)
        ).fetchone()
        return row["endpoint"] if row is not None else None

    def remove_endpoint(self, endpoint: str) -> None:
        self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
