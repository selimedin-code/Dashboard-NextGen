"""Ticker and number normalization shared by the Excel and PDF parsers.

The custodian file uses Bloomberg-style tickers (`NVDA US`, `IFX GR`) and Swiss
number formatting with an apostrophe thousands separator (`139'971.00`), and the
apostrophe may be a straight quote or a typographic one depending on the export.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Sentinel ticker for the cash line. Stored verbatim; treated as a position with
# avg_cost == 1 per the roadmap.
CASH_TICKER = "USD Cash"

# Any apostrophe-like thousands separator the export might emit.
_APOSTROPHES = "'’‘`´"
_NUM_STRIP = re.compile(f"[{re.escape(_APOSTROPHES)}\\s%$]")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
# Two-letter exchange suffix, e.g. the "US" in "NVDA US" or "GR" in "IFX GR".
_EXCHANGE_SUFFIX_RE = re.compile(r"^([A-Z][A-Z0-9.]*)\s+([A-Z]{2})$")


class ParseError(ValueError):
    """Raised when a value cannot be coerced — surfaced to the user, never swallowed."""


def parse_decimal(value: object) -> Decimal:
    """Coerce a custodian numeric cell to Decimal.

    Handles the apostrophe thousands separator, stray percent/currency signs, and
    whitespace introduced by PDF kerning. Never goes through float — that is the
    whole point of the Decimal-end-to-end rule.
    """
    if value is None:
        raise ParseError("missing number")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Pass through the string form so we do not inherit binary-float noise.
        return Decimal(str(value))
    text = _NUM_STRIP.sub("", str(value))
    if text in {"", "n.a.", "N.A.", "-", "None"}:
        raise ParseError(f"not a number: {value!r}")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ParseError(f"not a number: {value!r}") from exc


def is_cash_ticker(raw: str) -> bool:
    return raw.strip().upper().replace("  ", " ").startswith("USD CASH") or raw.strip().upper() == "USD"


def normalize_ticker(raw: str) -> tuple[str, str | None]:
    """Split a Bloomberg-style ticker into (normalized_ticker, exchange).

    `NVDA US` -> ("NVDA", "US");  `IFX GR` -> ("IFX", "GR");  `PLTR` -> ("PLTR", None).
    The country suffix is stripped for downstream API calls but returned so the
    caller can persist it — quoting `IFX GR` against a US endpoint returns the
    wrong price or nothing.
    """
    raw = " ".join(raw.split())  # collapse whitespace
    if is_cash_ticker(raw):
        return CASH_TICKER, None
    m = _EXCHANGE_SUFFIX_RE.match(raw)
    if m:
        return m.group(1), m.group(2)
    return raw, None


def validate_isin(isin: str | None) -> str | None:
    """Return the ISIN if it is well-formed, else None. Shape check only — this is
    a validated attribute, not a key, so a bad ISIN is a warning, not a failure."""
    if not isin:
        return None
    isin = isin.strip().upper()
    return isin if _ISIN_RE.match(isin) else None


def looks_like_ticker(token: str) -> bool:
    return bool(_TICKER_RE.match(token))
