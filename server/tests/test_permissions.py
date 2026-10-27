import pytest

from hearthkeep import db
from hearthkeep.permissions import (
    DEFAULT_LEVELS,
    SANDBOXED_TOOLS,
    ActionClass,
    Decision,
    ToolLevel,
    decide,
    effective_level,
    get_level,
    is_widening,
    set_level,
)

OFF, ASK, ALLOWED = ToolLevel.OFF, ToolLevel.ASK, ToolLevel.ALLOWED
LOOK, CHANGE_LOCAL, OUTWARD = ActionClass.LOOK, ActionClass.CHANGE_LOCAL, ActionClass.OUTWARD
RUN, DASK, STEP_UP, DENY = Decision.RUN, Decision.ASK, Decision.ASK_STEP_UP, Decision.DENY


EXPECTED = {
    (OFF, LOOK): (DENY, DENY),
    (OFF, CHANGE_LOCAL): (DENY, DENY),
    (OFF, OUTWARD): (DENY, DENY),
    (ASK, LOOK): (DASK, DASK),
    (ASK, CHANGE_LOCAL): (DASK, DASK),
    (ASK, OUTWARD): (STEP_UP, STEP_UP),
    (ALLOWED, LOOK): (RUN, RUN),
    (ALLOWED, CHANGE_LOCAL): (DASK, RUN),
    (ALLOWED, OUTWARD): (STEP_UP, STEP_UP),
}


@pytest.mark.parametrize(
    "level,action,granted,expected",
    [
        (lv, ac, g, EXPECTED[(lv, ac)][int(g)])
        for lv in ToolLevel
        for ac in ActionClass
        for g in (False, True)
    ],
)
def test_decide_matrix(level, action, granted, expected):
    assert decide(level, action, granted) == expected


@pytest.mark.parametrize("granted", [False, True])
@pytest.mark.parametrize(
    "level,action",
    [
        ("bogus", LOOK),
        (ALLOWED, "bogus"),
        (ASK, "bogus"),
        (OFF, "bogus"),
        ("bogus", "bogus"),
        ("bogus", OUTWARD),
    ],
)
def test_decide_fails_closed_on_unrecognised_values(level, action, granted):
    assert decide(level, action, granted) == DENY


def test_composed_python_without_docker_asks_with_docker_runs():
    no_docker = effective_level("python", ALLOWED, False)
    assert decide(no_docker, LOOK, False) == DASK
    with_docker = effective_level("python", ALLOWED, True)
    assert decide(with_docker, LOOK, False) == RUN


def test_set_level_rejects_invalid_level(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(ValueError):
        set_level(conn, "wallet", "bogus")
    assert conn.execute("SELECT COUNT(*) FROM tool_levels").fetchone()[0] == 0


def test_python_forced_to_ask_without_docker():
    assert effective_level("python", ALLOWED, False) == ASK
    assert effective_level("github_tests", ALLOWED, False) == ASK
    assert effective_level("python", ALLOWED, True) == ALLOWED
    assert effective_level("images", ALLOWED, False) == ALLOWED
    assert effective_level("python", OFF, False) == OFF
    assert effective_level("python", ASK, False) == ASK
    assert SANDBOXED_TOOLS == {"python", "github_tests"}


def test_is_widening():
    assert is_widening(OFF, ASK)
    assert is_widening(ASK, ALLOWED)
    assert is_widening(OFF, ALLOWED)
    assert not is_widening(ASK, OFF)
    assert not is_widening(ALLOWED, ASK)
    assert not is_widening(ALLOWED, OFF)
    for lvl in ToolLevel:
        assert not is_widening(lvl, lvl)


def test_defaults_v1():
    assert DEFAULT_LEVELS == {
        "wallet": ALLOWED,
        "news": ALLOWED,
        "files": ALLOWED,
        "python": ALLOWED,
        "images": ALLOWED,
        "send": ALLOWED,
        "github": ASK,
        "demo": ALLOWED,
    }


def test_unknown_tool_defaults_to_ask(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert get_level(conn, "mystery") == ASK
    assert get_level(conn, "wallet") == ALLOWED
    assert get_level(conn, "github") == ASK


def test_set_level_upserts(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    set_level(conn, "wallet", OFF)
    assert get_level(conn, "wallet") == OFF
    set_level(conn, "wallet", ASK)
    assert get_level(conn, "wallet") == ASK
    assert conn.execute("SELECT COUNT(*) FROM tool_levels").fetchone()[0] == 1


def test_invalid_stored_level_treated_as_ask(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO tool_levels(tool, level) VALUES ('wallet', 'bogus')")
    assert get_level(conn, "wallet") == ASK
    conn.execute("UPDATE tool_levels SET level = NULL WHERE tool = 'wallet'")
    assert get_level(conn, "wallet") == ASK
