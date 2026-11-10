"""Page through the CRM API with retries, then validate every record.

Retry policy (hand-rolled rather than tenacity so the rules are explicit and
testable with a fake ``sleep``):

* 429: wait exactly what ``Retry-After`` says (seconds or an HTTP date). The
  server knows its rate window; guessing shorter just burns another request.
* 5xx and network errors: exponential backoff with "full jitter",
  ``uniform(0, min(cap, base * 2**attempt))``. Jitter spreads out clients that
  all failed at the same moment so they do not retry in lockstep.
* Other 4xx: not retried. A bad request will not get better by repeating it.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from pipeline.models import Reject, dedupe_latest, validate_records

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ExtractError(RuntimeError):
    """The API kept failing after all retries, or returned a non-retryable error."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 6
    base_delay: float = 0.5
    max_delay: float = 30.0
    # Upper bound on a server-supplied Retry-After, so a misbehaving server
    # cannot park the job for an hour.
    max_retry_after: float = 120.0


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After is either delta-seconds ("2") or an HTTP date (RFC 9110)."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = now or datetime.now(UTC)
    return max(0.0, (when - now).total_seconds())


@dataclass
class CRMClient:
    http: httpx.Client
    page_size: int = 100
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random = field(default_factory=random.Random)
    request_count: int = 0  # observable in tests and logs

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self.retry.max_delay, self.retry.base_delay * 2**attempt)
        return self.rng.uniform(0, ceiling)

    def get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_problem = ""
        for attempt in range(self.retry.max_attempts):
            self.request_count += 1
            try:
                response = self.http.get(path, params=params)
            except httpx.TransportError as exc:
                last_problem = f"{type(exc).__name__}: {exc}"
                delay = self._backoff(attempt)
            else:
                if response.status_code < 400:
                    return response.json()
                if response.status_code not in RETRYABLE_STATUS:
                    raise ExtractError(
                        f"GET {path} -> {response.status_code}: {response.text[:200]}"
                    )
                last_problem = f"HTTP {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if response.status_code == 429 and retry_after is not None:
                    delay = min(retry_after, self.retry.max_retry_after)
                else:
                    delay = self._backoff(attempt)
            if attempt + 1 < self.retry.max_attempts:
                log.warning("GET %s failed (%s); retrying in %.2fs", path, last_problem, delay)
                self.sleep(delay)
        raise ExtractError(
            f"GET {path} failed after {self.retry.max_attempts} attempts ({last_problem})"
        )

    def iter_pages(
        self, resource: str, updated_since: datetime | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        params: dict[str, Any] = {"limit": self.page_size}
        if updated_since is not None:
            # Stored watermarks are naive UTC; send an explicit offset.
            params["updated_since"] = updated_since.replace(tzinfo=UTC).isoformat()
        while True:
            body = self.get_json(f"/{resource}", params)
            yield body["data"]
            cursor = body.get("next_cursor")
            if not cursor:
                return
            params["cursor"] = cursor


@dataclass
class ExtractResult:
    resource: str
    records: list[Any]  # validated pydantic models, deduplicated by id
    rejects: list[Reject]
    pages: int
    raw_count: int


def extract(
    client: CRMClient, resource: str, updated_since: datetime | None = None
) -> ExtractResult:
    raw: list[dict[str, Any]] = []
    pages = 0
    for page in client.iter_pages(resource, updated_since):
        raw.extend(page)
        pages += 1
    valid, rejects = validate_records(resource, raw)
    records = dedupe_latest(valid)
    log.info(
        "%s: %d raw, %d valid, %d rejected, %d pages",
        resource, len(raw), len(records), len(rejects), pages,
    )
    return ExtractResult(resource, records, rejects, pages, len(raw))
