import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest

from hearthkeep import audit
from hearthkeep.approvals import ApprovalError, ApprovalStore
from hearthkeep.db import connect
from hearthkeep.permissions import ActionClass


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
    return connect(tmp_path / "hk.db")


@pytest.fixture
def store(conn, clock):
    return ApprovalStore(conn, ttl_minutes=10, clock=clock, poll_interval=0.01)


def _create(store, step_up=False, tool="github"):
    return store.create(
        "chat1", "turn1", tool, ActionClass.OUTWARD, "Push branch", {"branch": "x"}, step_up
    )


def test_create_pending(store, clock):
    a = _create(store, step_up=True)
    assert a.status == "pending"
    assert a.detail == {"branch": "x"}
    assert a.requires_step_up is True
    assert a.action_class == "outward"
    assert len(a.id) >= 16
    assert datetime.fromisoformat(a.expires_at) == clock.now + timedelta(minutes=10)
    assert store.get(a.id) == a
    assert [p.id for p in store.pending()] == [a.id]


def test_approve_with_step_up_when_required(store):
    a = _create(store, step_up=True)
    r = store.resolve(a.id, "sess1", approve=True, step_up_verified=True)
    assert r.status == "approved"
    assert store.get(a.id).status == "approved"
    assert store.pending() == []


def test_step_up_required_rejected_without_verification(store):
    a = _create(store, step_up=True)
    with pytest.raises(ApprovalError) as e:
        store.resolve(a.id, "sess1", approve=True, step_up_verified=False)
    assert e.value.reason == "step_up_required"
    assert store.get(a.id).status == "pending"


def test_decline_never_needs_step_up(store):
    a = _create(store, step_up=True)
    assert store.resolve(a.id, "sess1", approve=False, step_up_verified=False).status == "declined"


def test_not_found(store):
    with pytest.raises(ApprovalError) as e:
        store.resolve("nope", "s", approve=True, step_up_verified=True)
    assert e.value.reason == "not_found"
    with pytest.raises(ApprovalError) as e:
        store.get("nope")
    assert e.value.reason == "not_found"


def test_resolve_twice_rejected(store):
    a = _create(store)
    store.resolve(a.id, "s", approve=True, step_up_verified=False)
    with pytest.raises(ApprovalError) as e:
        store.resolve(a.id, "s", approve=False, step_up_verified=False)
    assert e.value.reason == "already_resolved"
    assert store.get(a.id).status == "approved"


def test_resolve_after_expiry_rejected(store, clock):
    a = _create(store)
    clock.advance(minutes=11)
    with pytest.raises(ApprovalError) as e:
        store.resolve(a.id, "s", approve=True, step_up_verified=True)
    assert e.value.reason == "expired"
    assert store.get(a.id).status == "expired"


def test_get_and_pending_report_expired(store, clock):
    a = _create(store)
    clock.advance(minutes=11)
    assert store.pending() == []
    assert store.get(a.id).status == "expired"


def test_concurrent_resolve_only_one_wins(tmp_path, clock):
    path = tmp_path / "hk.db"
    first = ApprovalStore(connect(path), 10, clock)
    a = _create(first)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def worker(approve):
        s = ApprovalStore(connect(path), 10, clock)
        barrier.wait()
        try:
            outcomes.append(s.resolve(a.id, "s", approve=approve, step_up_verified=True))
        except ApprovalError as err:
            outcomes.append(err)

    threads = [threading.Thread(target=worker, args=(v,)) for v in (True, False)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [o for o in outcomes if not isinstance(o, ApprovalError)]
    losses = [o for o in outcomes if isinstance(o, ApprovalError)]
    assert len(wins) == 1 and len(losses) == 1
    assert losses[0].reason == "already_resolved"
    assert first.get(a.id).status == wins[0].status


def test_wait_returns_on_resolve(store):
    a = _create(store)

    async def scenario():
        task = asyncio.create_task(store.wait(a.id))
        await asyncio.sleep(0.05)
        assert not task.done()
        store.resolve(a.id, "s", approve=True, step_up_verified=False)
        return await asyncio.wait_for(task, 2)

    assert asyncio.run(scenario()).status == "approved"


def test_wait_returns_expired(store, clock):
    a = _create(store)

    async def scenario():
        task = asyncio.create_task(store.wait(a.id))
        await asyncio.sleep(0.03)
        clock.advance(minutes=11)
        return await asyncio.wait_for(task, 2)

    assert asyncio.run(scenario()).status == "expired"


def test_resolutions_are_audited(store, conn):
    a = _create(store)
    store.resolve(a.id, "sess-secret-id", approve=True, step_up_verified=False)
    events = {e["event"]: e for e in audit.list_events(conn)}
    created = events["approval.created"]
    assert created["actor"] == "agent"
    assert created["detail"]["approval_id"] == a.id
    resolved = events["approval.resolved"]
    assert resolved["actor"] == "session:sess-secret-id"
    assert resolved["detail"] == {"approval_id": a.id, "status": "approved", "tool": "github"}
    assert "sess-secret-id" not in str(resolved["detail"])
