from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class MockConfig:
    """Knobs for the fake CRM.

    Failure injection is counter based rather than random so tests can predict
    exactly which request fails: with ``rate_limit_every=3`` requests 3, 6, 9, ...
    get a 429. A retried request is a new request, so it gets a fresh number
    and (for every >= 2) cannot fail twice in a row.
    """

    seed: int = 42
    n_contacts: int = 150
    n_deals: int = 400
    n_activities: int = 900
    # The synthetic data ends at this instant. Deal creation dates are spread
    # over the 14 weeks before it, so the demo week 2026-11-02 has full data.
    data_end: datetime = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)
    history_weeks: int = 14

    default_page_size: int = 50
    max_page_size: int = 200

    # Every Nth request returns 429 with a Retry-After header. None = off.
    rate_limit_every: int | None = None
    retry_after_seconds: int = 1
    # Every Nth request returns 503. None = off.
    fail_every: int | None = None
    # Fraction of records served with a deliberate data-quality defect
    # (bad email, negative amount, unknown stage, ...) to exercise rejects.
    corrupt_rate: float = 0.0

    def __post_init__(self) -> None:
        for name in ("rate_limit_every", "fail_every"):
            value = getattr(self, name)
            if value is not None and value < 2:
                # every=1 would fail every retry too, which is an outage, not a blip.
                raise ValueError(f"{name} must be >= 2 (or None)")
        if not 0.0 <= self.corrupt_rate <= 1.0:
            raise ValueError("corrupt_rate must be between 0 and 1")
