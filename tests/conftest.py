from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from mock_crm import CRMStore, MockConfig, create_app
from pipeline.extract import CRMClient, RetryPolicy


class SleepRecorder:
    """Stands in for time.sleep: records requested delays, never waits."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def make_client(sleeps: SleepRecorder) -> Callable[..., tuple[CRMClient, CRMStore]]:
    """Build a CRMClient talking to an in-process mock CRM (no network)."""

    def _make(page_size: int = 50, **config_kwargs: object) -> tuple[CRMClient, CRMStore]:
        store = CRMStore(MockConfig(**config_kwargs))  # type: ignore[arg-type]
        http = TestClient(create_app(store=store))
        client = CRMClient(
            http=http,
            page_size=page_size,
            retry=RetryPolicy(max_attempts=5, base_delay=0.5, max_delay=8),
            sleep=sleeps,
            rng=random.Random(0),
        )
        return client, store

    return _make



@pytest.fixture
def pg_engine():
    """A clean PostgreSQL schema, only for tests marked `integration`."""
    import os

    from sqlalchemy import create_engine

    from pipeline.load import metadata

    url = os.environ.get("TEST_PG_URL")
    if not url:
        pytest.skip("TEST_PG_URL not set")
    eng = create_engine(url)
    metadata.drop_all(eng)
    metadata.create_all(eng)
    yield eng
    eng.dispose()
