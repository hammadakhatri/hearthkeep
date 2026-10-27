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
