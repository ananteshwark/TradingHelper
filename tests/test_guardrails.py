from __future__ import annotations

from pathlib import Path

import pytest

from igs.guardrails import (
    DISCLAIMER,
    AdviceLanguageError,
    assert_no_advice_language,
    find_advice_language,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("text", [
    "Strong Buy on dips", "We would sell here", "Target price of Rs 1,200",
    "price target 500", "upside of 40%", "a multibagger in the making",
    "entry level 350, stop-loss 320", "Accumulate on weakness",
])
def test_advice_language_is_caught(text):
    assert find_advice_language(text)
    with pytest.raises(AdviceLanguageError):
        assert_no_advice_language(text)


@pytest.mark.parametrize("text", [
    "Tier: High conviction. ROCE 24.1% (82nd percentile of peers, source: Q1 FY26 results).",
    "Rejected: promoter pledge 31% of holding exceeds the 20% threshold.",
    "Revenue 3y CAGR 18.2% vs industry median 11.0%.",
    DISCLAIMER,
])
def test_neutral_research_language_passes(text):
    assert assert_no_advice_language(text) == text


def test_readme_carries_disclaimer_and_sebi_note():
    readme = (REPO / "README.md").read_text()
    assert DISCLAIMER in readme
    assert "Research Analyst" in readme and "SEBI" in readme
