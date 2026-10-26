import json
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeVerifier

from hearthkeep import audit
from hearthkeep.auth.enrollment import CODE_ALPHABET, EnrollmentCodes, first_device_allowed
from hearthkeep.auth.sessions import SessionStore, StepUp
from hearthkeep.auth.webauthn import (
    ChallengeStore,
    PyWebAuthnVerifier,
    StoredCredential,
    VerificationError,
    b64url_decode,
)
from hearthkeep.config import Settings
from hearthkeep.db import connect


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "hk.db")
    yield c
    c.close()


def add_credential(conn, cred_id="cred-1"):
    conn.execute(
        "INSERT INTO credentials (id, public_key, sign_count, device_name, created_at)"
        " VALUES (?, ?, 0, 'laptop', '2026-01-01T00:00:00+00:00')",
        (cred_id, b"pk"),
    )


def audit_text(conn):
    return json.dumps(audit.list_events(conn, limit=1000))


# ---- enrollment codes ----


def test_first_device_code_only_when_no_credentials(conn, clock):
    codes = EnrollmentCodes(conn, clock=clock)
    assert first_device_allowed(conn) is True
    assert codes.first_device_allowed() is True
    code = codes.issue("first_device", ttl_minutes=15)
    add_credential(conn)
    assert first_device_allowed(conn) is False
    # a first-device code cannot be redeemed once a credential exists
    assert codes.consume(code, "first_device") is False
    # a revoked credential does not count
    conn.execute("UPDATE credentials SET revoked_at = '2026-01-02T00:00:00+00:00'")
    assert first_device_allowed(conn) is True
    assert codes.consume(code, "first_device") is True


def test_code_format_and_case_insensitive_consume(conn, clock):
    codes = EnrollmentCodes(conn, clock=clock)
    code = codes.issue("add_device", ttl_minutes=10)
    assert len(code) == 8 and set(code) <= set(CODE_ALPHABET)
    assert codes.consume(code.lower(), "add_device") is True


def test_code_single_use(conn, clock):
    codes = EnrollmentCodes(conn, clock=clock)
    code = codes.issue("add_device", ttl_minutes=10)
    assert codes.consume(code, "add_device") is True
    assert codes.consume(code, "add_device") is False


def test_code_wrong_purpose_or_unknown_rejected(conn, clock):
    codes = EnrollmentCodes(conn, clock=clock)
    code = codes.issue("add_device", ttl_minutes=10)
    assert codes.consume(code, "first_device") is False
    assert codes.consume("AAAAAAAA", "add_device") is False
    assert codes.consume(code, "add_device") is True  # failed attempts did not burn it


def test_code_expires(conn, clock):
    codes = EnrollmentCodes(conn, clock=clock)
    code = codes.issue("add_device", ttl_minutes=10)
    clock.advance(minutes=10)
    assert codes.consume(code, "add_device") is False
    fresh = codes.issue("add_device", ttl_minutes=10)
    clock.advance(minutes=9, seconds=59)
    assert codes.consume(fresh, "add_device") is True


def test_code_stored_hashed(conn, clock):
    codes = EnrollmentCodes(conn, clock=clock)
    code = codes.issue("add_device", ttl_minutes=10)
    stored = conn.execute("SELECT code_hash FROM enrollment_codes").fetchone()[0]
    assert code.lower() not in stored.lower()
    assert len(stored) == 64
    codes.consume(code, "add_device")
    events = audit.list_events(conn)
    assert {e["event"] for e in events} >= {"enrollment.code_issued", "enrollment.code_used"}
    assert code not in audit_text(conn)
    issued = next(e for e in events if e["event"] == "enrollment.code_issued")
    assert issued["detail"] == {"purpose": "add_device"}


# ---- sessions ----


def test_session_create_and_validate(conn, clock):
    add_credential(conn)
    store = SessionStore(conn, idle_days=14, clock=clock)
    sid = store.create("cred-1")
    assert len(sid) >= 43
    assert sid not in [r[0] for r in conn.execute("SELECT id FROM sessions")]
    session = store.validate(sid)
    assert session is not None and session.credential_id == "cred-1" and session.id == sid
    assert store.validate("nope") is None
    assert sid not in audit_text(conn)
    assert "session.created" in {e["event"] for e in audit.list_events(conn)}


def test_session_idle_expiry(conn, clock):
    add_credential(conn)
    store = SessionStore(conn, idle_days=14, clock=clock)
    sid = store.create("cred-1")
    clock.advance(days=13)
    assert store.validate(sid) is not None  # activity refreshes last_seen_at
    clock.advance(days=13)
    assert store.validate(sid) is not None
    clock.advance(days=14, seconds=1)
    assert store.validate(sid) is None
    clock.advance(days=1)
    assert store.validate(sid) is None  # an expired session stays dead


def test_session_revoke(conn, clock):
    add_credential(conn)
    store = SessionStore(conn, idle_days=14, clock=clock)
    sid = store.create("cred-1")
    store.revoke(sid)
    assert store.validate(sid) is None
    assert "session.revoked" in {e["event"] for e in audit.list_events(conn)}
    assert sid not in audit_text(conn)


def test_revoking_credential_kills_sessions(conn, clock):
    add_credential(conn, "cred-1")
    add_credential(conn, "cred-2")
    store = SessionStore(conn, idle_days=14, clock=clock)
    a, b, other = store.create("cred-1"), store.create("cred-1"), store.create("cred-2")
    store.revoke_credential("cred-1")
    assert store.validate(a) is None and store.validate(b) is None
    assert store.validate(other) is not None
    revoked_at = conn.execute("SELECT revoked_at FROM credentials WHERE id='cred-1'").fetchone()[0]
    assert revoked_at is not None
    events = {e["event"] for e in audit.list_events(conn)}
    assert {"credential.revoked", "session.revoked"} <= events


def test_session_of_revoked_credential_rejected_even_if_session_row_live(conn, clock):
    add_credential(conn)
    store = SessionStore(conn, idle_days=14, clock=clock)
    sid = store.create("cred-1")
    conn.execute("UPDATE credentials SET revoked_at = '2026-01-01T13:00:00+00:00'")
    assert store.validate(sid) is None


# ---- step-up ----


def test_step_up_token_bound_to_session_and_approval(clock):
    step = StepUp(clock=clock)
    token = step.issue("sess-1", "appr-1", "approve")
    assert step.check(token, "sess-2", "appr-1", "approve") is False
    token = step.issue("sess-1", "appr-1", "approve")
    assert step.check(token, "sess-1", "appr-2", "approve") is False
    token = step.issue("sess-1", "appr-1", "approve")
    assert step.check(token, "sess-1", "appr-1", "other-purpose") is False
    token = step.issue("sess-1", "appr-1", "approve")
    assert step.check(token, "sess-1", None, "approve") is False
    token = step.issue("sess-1", "appr-1", "approve")
    assert step.check(token, "sess-1", "appr-1", "approve") is True


def test_step_up_token_without_approval(clock):
    step = StepUp(clock=clock)
    token = step.issue("sess-1", None, "settings")
    assert step.check(token, "sess-1", "appr-1", "settings") is False
    token = step.issue("sess-1", None, "settings")
    assert step.check(token, "sess-1", None, "settings") is True


def test_step_up_token_single_use_and_expires(clock):
    step = StepUp(clock=clock)
    token = step.issue("s", "a", "p")
    assert step.check(token, "s", "a", "p") is True
    assert step.check(token, "s", "a", "p") is False
    token = step.issue("s", "a", "p")
    clock.advance(seconds=59)
    assert step.check(token, "s", "a", "p") is True
    token = step.issue("s", "a", "p")
    clock.advance(seconds=60)
    assert step.check(token, "s", "a", "p") is False
    assert step.check("garbage", "s", "a", "p") is False


# ---- challenges and verifiers ----


def test_challenge_store_single_use_and_ttl(clock):
    store = ChallengeStore(clock=clock)
    cid = store.put(b"chal")
    assert store.take(cid) == b"chal"
    assert store.take(cid) is None
    cid = store.put(b"chal")
    clock.advance(minutes=5)
    assert store.take(cid) is None
    assert store.take("unknown") is None


def test_fake_verifier_round_trip():
    v = FakeVerifier()
    opts = v.registration_options(b"owner")
    challenge = b64url_decode(opts["challenge"])
    new = v.verify_registration({"id": "c", "public_key": b"k", "challenge": challenge}, challenge)
    assert new.id == "c"
    with pytest.raises(VerificationError):
        v.verify_registration({"id": "c", "public_key": b"k", "challenge": b"x"}, challenge)


def _settings(tmp_path):
    return Settings(data_dir=tmp_path, rp_id="localhost", public_origin="http://localhost:8765")


def test_pywebauthn_registration_options(tmp_path):
    verifier = PyWebAuthnVerifier(_settings(tmp_path))
    opts = verifier.registration_options(b"owner-id")
    assert opts["rp"] == {"name": "Hearthkeep", "id": "localhost"}
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    assert len(b64url_decode(opts["challenge"])) >= 32


def test_pywebauthn_authentication_options(tmp_path):
    verifier = PyWebAuthnVerifier(_settings(tmp_path))
    cid = "AQIDBA"
    opts = verifier.authentication_options([cid])
    assert opts["rpId"] == "localhost"
    assert opts["userVerification"] == "required"
    assert [c["id"] for c in opts["allowCredentials"]] == [cid]


def test_pywebauthn_rejects_garbage_response(tmp_path):
    verifier = PyWebAuthnVerifier(_settings(tmp_path))
    with pytest.raises(VerificationError):
        verifier.verify_registration({"nonsense": True}, b"c" * 32)
    stored = StoredCredential(id="AQIDBA", public_key=b"k", sign_count=0)
    with pytest.raises(VerificationError):
        verifier.verify_authentication({"nonsense": True}, b"c" * 32, stored)
