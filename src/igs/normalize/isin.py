"""ISIN helpers (ISO 6166).

Indian ISINs look like INE009A01021:
    IN     country
    E      issuer type (E = company; other characters for other issuer kinds)
    009A   issuer code, assigned to the company by the depository
    01     security type
    02     issue serial; changes on events such as a face-value split
    1      check digit

Two ISINs that share the first nine characters are the same issuer and
security type, differing only in issue serial. That is the evidence used to
link an ISIN change under an unchanged NSE symbol into one security chain.
"""

from __future__ import annotations

import re

_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def is_valid_isin(isin: str) -> bool:
    if not isinstance(isin, str) or not _ISIN_RE.match(isin):
        return False
    digits = "".join(str(int(ch, 36)) for ch in isin[:-1])
    total = 0
    # Luhn over the expanded digit string, doubling from the rightmost digit.
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10 == int(isin[-1])


def issue_prefix(isin: str) -> str:
    """Issuer + security type: identical across a face-value split's ISIN change."""
    return isin[:9]


def issuer_code(isin: str) -> str:
    return isin[3:7]
