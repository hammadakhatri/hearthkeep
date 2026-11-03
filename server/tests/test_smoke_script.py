"""The real-model smoke's analysis verdict: strict enough to catch a confident wrong answer."""

import pytest
from smoke_e2e import SmokeError, judge_analysis

GOOD_TEXT = "North has no rows from 12 to 24 March."


def mark(text, kind="computed"):
    return {"text": text, "kind": kind}


def test_a_reply_with_the_gap_as_computed_numbers_passes():
    checks = [{"text": GOOD_TEXT, "marks": [mark("12"), mark("24")]}]
    assert judge_analysis(checks, []) == "(computed 2, unverified 0)"


def test_run_values_with_the_gap_dates_pass_too():
    checks = [{"text": "North is missing 13 days.", "marks": [mark("13"), mark("3", "unverified")]}]
    values = [{"value": "2025-03-12"}, {"value": "2025-03-24T00:00:00"}]
    assert judge_analysis(checks, values) == "(computed 1, unverified 1)"


@pytest.mark.parametrize(
    ("checks", "values", "why"),
    [
        ([{"text": GOOD_TEXT, "marks": [mark("12", "unverified")]}], [], "no computed"),
        ([{"text": "North dips in March.", "marks": [mark("3")]}], [], "12 to 24 March"),
        (
            [{"text": "Total hk.result(98.46, 'amount').", "marks": [mark("12"), mark("24")]}],
            [],
            "hk.",
        ),
        ([], [], "no number_check"),
    ],
)
def test_a_wrong_or_unchecked_answer_fails(checks, values, why):
    with pytest.raises(SmokeError, match=why):
        judge_analysis(checks, values)


def test_only_the_final_reply_counts():
    early = {"text": GOOD_TEXT, "marks": [mark("12"), mark("24")]}
    final = {"text": "Something else entirely.", "marks": [mark("7")]}
    with pytest.raises(SmokeError, match="12 to 24 March"):
        judge_analysis([early, final], [])


class _Health:
    def json(self):
        return {"chat_model_reachable": True}


@pytest.fixture
def no_server(monkeypatch):
    """main() with a fake server: health answers, sign-in passes, every step is ours."""
    import smoke_e2e

    monkeypatch.setattr(smoke_e2e.Client, "get", lambda self, path: _Health())
    monkeypatch.setattr(smoke_e2e, "sign_in", lambda client, code: None)
    return smoke_e2e


@pytest.mark.parametrize("error", [SmokeError("wrong answer"), RuntimeError("bug in a step")])
def test_any_fail_line_means_a_non_zero_exit(no_server, monkeypatch, capsys, error):
    def boom(client):
        raise error

    monkeypatch.setattr(no_server, "check_clock", boom)
    monkeypatch.setattr(no_server, "check_note_approval", lambda client: "")
    assert no_server.main([]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL clock tool: ")
    assert "OK note approval" in out


def test_all_ok_exits_zero(no_server, monkeypatch, capsys):
    monkeypatch.setattr(no_server, "check_clock", lambda client: "")
    monkeypatch.setattr(no_server, "check_note_approval", lambda client: "")
    assert no_server.main([]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_the_script_exits_non_zero_when_it_prints_fail():
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "smoke_e2e.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--base", "http://127.0.0.1:9"],
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert proc.stdout.startswith("FAIL ")
    assert proc.returncode != 0


def test_changed_lines_ignore_file_headers_and_order():
    from smoke_e2e import changed_lines

    one = "--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n keep\n+added\n-gone\n"
    two = "diff --git a/README.md b/README.md\n+++ b/README.md\n--- a/README.md\n-gone\n+added\n"
    assert changed_lines(one) == changed_lines(two) == ["+added", "-gone"]


def test_github_steps_are_skipped_without_a_repo(no_server, monkeypatch, capsys):
    monkeypatch.delenv("HK_SMOKE_GITHUB_REPO", raising=False)
    monkeypatch.setattr(no_server, "check_clock", lambda client: "")
    monkeypatch.setattr(no_server, "check_note_approval", lambda client: "")
    assert no_server.main([]) == 0
    assert "SKIP github" in capsys.readouterr().out


def test_a_repo_without_a_token_fails_the_github_steps(no_server, monkeypatch, capsys):
    monkeypatch.setenv("HK_SMOKE_GITHUB_REPO", "someone/throwaway")
    monkeypatch.delenv("HK_SMOKE_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(no_server, "check_clock", lambda client: "")
    monkeypatch.setattr(no_server, "check_note_approval", lambda client: "")
    assert no_server.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL github setup: HK_SMOKE_GITHUB_TOKEN is not set" in out
    assert "SKIP github edit" in out
    assert "someone/throwaway" not in out


def test_an_already_saved_token_is_never_replaced_or_removed(monkeypatch):
    import smoke_e2e

    class Connected:
        def json(self):
            return {"git": True, "connected": True}

    class Client:
        cred_id = "dev"
        http = None

        def get(self, path):
            return Connected()

        def post(self, path, body=None):
            raise AssertionError("no request may follow")

    state = {"gh_repo": "someone/throwaway", "gh_token": "github_pat_" + "a" * 30}
    with pytest.raises(SmokeError, match="already saved"):
        smoke_e2e.check_github_setup(Client(), state)
    assert "token_saved" not in state
    smoke_e2e.github_cleanup(Client(), state)  # nothing saved by this run: nothing removed
