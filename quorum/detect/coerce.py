"""Value coercion for structural comparison.

`"2026-09-14"` and `"Sep 14 2026"` and `{"date": "2026-09-14"}` all denote the
same day. If tier 1 compares them as raw strings it reports a contradiction
that is not there, or worse, misses one that is. This module is the single
place that decides what "the same value" means.

Pure: no I/O, no clock, no randomness. Every branch is unit-tested.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_TEXT_DATE = re.compile(r"^([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", re.I)
_TEXT_DATE_DMY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?,?\s+(\d{4})$", re.I)
_MONEY = re.compile(r"^\s*[-+]?[$£€]?\s*([0-9][0-9,_]*(?:\.[0-9]+)?)\s*(usd|eur|gbp)?\s*$", re.I)


def coerce_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    m = _ISO_DATE.match(s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _TEXT_DATE.match(s)
    if m and m.group(1).lower().rstrip(".") in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower().rstrip(".")], int(m.group(2)))
        except ValueError:
            return None
    m = _TEXT_DATE_DMY.match(s)
    if m and m.group(2).lower().rstrip(".") in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).lower().rstrip(".")], int(m.group(1)))
        except ValueError:
            return None
    return None


def coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None                       # True == 1 is never what we mean here
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    m = _MONEY.match(value)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").replace("_", ""))
    except ValueError:
        return None


def coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "y", "on", "1"):
            return True
        if s in ("false", "no", "n", "off", "0"):
            return False
    return None


def normalize_scalar(value: Any) -> Any:
    """Reduce a scalar to a canonical comparable form."""
    d = coerce_date(value)
    if d is not None:
        return ("date", d.isoformat())
    b = coerce_bool(value)
    if b is not None:
        return ("bool", b)
    n = coerce_number(value)
    if n is not None:
        return ("number", n)
    if isinstance(value, str):
        return ("string", " ".join(value.strip().lower().split()))
    return ("other", value)


def scalar_of(obj: Any) -> Any | None:
    """Extract the single scalar a claim's object_json denotes, if it has one.

    {"date": "2026-09-14"} -> "2026-09-14"
    {"value": 2400}        -> 2400
    "2026-09-14"           -> "2026-09-14"
    {"min": 1, "max": 5}   -> None  (a range, not a scalar)
    """
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        if len(obj) == 1:
            (only,) = obj.values()
            return only if isinstance(only, (str, int, float, bool)) else None
        for key in ("value", "date", "amount", "text"):
            if key in obj and isinstance(obj[key], (str, int, float, bool)):
                return obj[key]
    return None


def range_of(obj: Any) -> tuple[float | None, float | None] | None:
    """Extract a numeric range, if the object denotes one."""
    if not isinstance(obj, dict):
        return None
    lo_key = next((k for k in ("min", "low", "from", "gte", "at_least") if k in obj), None)
    hi_key = next((k for k in ("max", "high", "to", "lte", "at_most") if k in obj), None)
    if lo_key is None and hi_key is None:
        return None
    lo = coerce_number(obj[lo_key]) if lo_key else None
    hi = coerce_number(obj[hi_key]) if hi_key else None
    if lo is None and hi is None:
        return None
    return (lo, hi)


def values_equal(a: Any, b: Any) -> bool:
    return normalize_scalar(a) == normalize_scalar(b)


def point_in_range(point: Any, rng: tuple[float | None, float | None]) -> bool:
    n = coerce_number(point)
    if n is None:
        return False
    lo, hi = rng
    if lo is not None and n < lo:
        return False
    if hi is not None and n > hi:
        return False
    return True
