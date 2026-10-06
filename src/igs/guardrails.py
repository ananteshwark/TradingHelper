"""Advice-language guardrails.

This is a personal research tool. The screen's own output (rankings, tiers,
explanations, briefs, reports, alerts) must never read as a recommendation: no
buy/sell calls, no target prices. Every such string passes through
`assert_no_advice_language` (and tests scan the templates). The one exception is
the AI's buy / hold / sell calls (igs.assistant.calls), which the user asked for,
are labelled as the AI's judgement, and carry their own track record.
"""

from __future__ import annotations

import re

# Retained as an empty compatibility value for existing API consumers.
DISCLAIMER = ""

# Phrases that turn a screen into a call. Matched case-insensitively on word
# boundaries. The disclaimer itself is the one permitted mention.
_BANNED = [
    r"strong\s+buy", r"\bbuy\b", r"\bsell\b", r"\baccumulate\b", r"\bbook\s+profits?\b",
    r"target\s+price", r"price\s+target", r"\btarget\b\s*(?:of\s*)?(?:rs\.?|₹|inr)",
    r"\bupside\s+(?:of|to)\b", r"\bmultibagger\b", r"\bsure\s*shot\b", r"\bguaranteed\b",
    r"\bentry\s+(?:price|level|point)\b", r"\bstop[\s-]*loss\b", r"\bexit\s+(?:price|level)\b",
]
_BANNED_RE = re.compile("|".join(f"(?:{p})" for p in _BANNED), re.IGNORECASE)


class AdviceLanguageError(ValueError):
    pass


def find_advice_language(text: str) -> list[str]:
    return [m.group(0) for m in _BANNED_RE.finditer(text.replace(DISCLAIMER, ""))]


def assert_no_advice_language(text: str) -> str:
    hits = find_advice_language(text)
    if hits:
        raise AdviceLanguageError(f"advice language in generated output: {hits}")
    return text
