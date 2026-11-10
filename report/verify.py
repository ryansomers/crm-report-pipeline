"""Check that every figure in a summary can be traced to the computed metrics.

The rule: a number written in the summary is accepted only if it is a correct
rounding of some number in the metrics JSON, at the precision it was written.
"75%" matches 74.6; "$3,501" matches 3500.5; "$1.2M" matches 1,234,567;
"42" matches nothing unless some metric rounds to 42. Signs are ignored so
"down 3" can describe a delta of -3. ISO dates must appear verbatim.

This catches invented or mis-computed figures (the common LLM failure). It
does not prove each figure is attached to the right label: "won 3 deals" passes
if 3 is any metric. That would need per-claim citations; see README limitations.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# Optional sign and $, digits with optional thousands separators, optional
# fraction, optional % or k/M/B suffix. The lookbehind skips digits glued to
# words or decimals ("Q4", "v2", the "5" in "1.5" is part of the same match).
NUMBER = re.compile(
    r"(?<![\w.])[-+−]?\$?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?"
    r"(?P<suffix>\s?%|[kKmMbB](?![A-Za-z]))?"
)
MULTIPLIERS = {"k": 1e3, "m": 1e6, "b": 1e9}


@dataclass(frozen=True)
class Figure:
    text: str
    value: float
    decimals: int
    multiplier: float


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    unsupported: list[str]
    checked: int


def extract_figures(text: str) -> tuple[list[str], list[Figure]]:
    """Return (iso_dates, numeric_figures) found in ``text``."""
    dates = ISO_DATE.findall(text)
    text = ISO_DATE.sub(" ", text)
    figures = []
    for m in NUMBER.finditer(text):
        frac = m.group("frac") or ""
        suffix = (m.group("suffix") or "").strip().lower()
        figures.append(
            Figure(
                text=m.group(0).strip(),
                value=float(m.group("int").replace(",", "") + (f".{frac}" if frac else "")),
                decimals=len(frac),
                multiplier=MULTIPLIERS.get(suffix, 1.0),
            )
        )
    return dates, figures


def _walk(obj: Any) -> Iterator[Any]:
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def metric_values(metrics: dict[str, Any]) -> tuple[set[str], list[float]]:
    dates: set[str] = set()
    numbers: list[float] = []
    for leaf in _walk(metrics):
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, int | float):
            numbers.append(abs(float(leaf)))
        elif isinstance(leaf, str):
            dates.update(ISO_DATE.findall(leaf))
    return dates, numbers


def is_supported(fig: Figure, numbers: list[float]) -> bool:
    # x is a valid rounding of v at d decimals iff |v - x| <= half a unit in
    # the last written place. The epsilon absorbs float representation error.
    tolerance = 0.5 * 10 ** (-fig.decimals) + 1e-9
    return any(abs(v / fig.multiplier - fig.value) <= tolerance for v in numbers)


def verify_text(text: str, metrics: dict[str, Any]) -> VerificationResult:
    allowed_dates, numbers = metric_values(metrics)
    dates, figures = extract_figures(text)
    unsupported = [d for d in dates if d not in allowed_dates]
    unsupported += [f.text for f in figures if not is_supported(f, numbers)]
    return VerificationResult(
        ok=not unsupported, unsupported=unsupported, checked=len(dates) + len(figures)
    )
