from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from mock_crm.data import RESOURCES
from pipeline.extract import CRMClient, ExtractError, RetryPolicy, extract, parse_retry_after
from pipeline.models import Deal, dedupe_latest, validate_records


@pytest.mark.parametrize("resource", RESOURCES)
def test_pagination_collects_every_record_exactly_once(make_client, resource):
    # 7 does not divide any resource size, so the last page is partial.
    client, store = make_client(page_size=7, n_contacts=40, n_deals=60, n_activities=90)
    result = extract(client, resource)

    ids = [r.id for r in result.records]
    expected = {r["id"] for r in store.records[resource]}
    assert result.raw_count == len(expected)  # nothing served twice
    assert len(ids) == len(set(ids))
    assert set(ids) == expected
    assert result.pages == math.ceil(len(expected) / 7)


def test_page_size_is_capped_by_server(make_client):
    client, store = make_client(page_size=10_000, n_deals=450, max_page_size=200)
    result = extract(client, "deals")
    assert result.raw_count == 450
    assert result.pages == 3


def test_updated_since_returns_only_changed_records(make_client):
    client, store = make_client(page_size=25)
    watermark = store.config.data_end.replace(tzinfo=None)
    changed = store.simulate_changes(5)

    result = extract(client, "deals", updated_since=watermark + timedelta(seconds=1))
    assert sorted(r.id for r in result.records) == sorted(changed)


def test_rate_limit_honours_retry_after(make_client, sleeps):
    client, store = make_client(
        page_size=20, n_deals=100, rate_limit_every=2, retry_after_seconds=3
    )
    result = extract(client, "deals")

    assert {r.id for r in result.records} == {d["id"] for d in store.records["deals"]}
    # Every other request was a 429; each one waited exactly Retry-After.
    assert sleeps.calls and all(s == 3.0 for s in sleeps.calls)
    assert client.request_count == result.pages + len(sleeps.calls)


def test_server_errors_back_off_with_jitter(make_client, sleeps):
    client, store = make_client(page_size=20, n_deals=100, fail_every=3)
    result = extract(client, "deals")

    assert result.raw_count == 100
    assert sleeps.calls
    # First retry of any request is attempt 0: delay in [0, base * 2**0].
    assert all(0 <= s <= client.retry.max_delay for s in sleeps.calls)


def test_retries_exhausted_raises(sleeps):
    calls = {"n": 0}

    def always_503(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    client = CRMClient(
        http=httpx.Client(transport=httpx.MockTransport(always_503), base_url="http://crm"),
        retry=RetryPolicy(max_attempts=4, base_delay=1, max_delay=10),
        sleep=sleeps,
        rng=random.Random(1),
    )
    with pytest.raises(ExtractError, match="after 4 attempts"):
        extract(client, "deals")
    assert calls["n"] == 4
    assert len(sleeps.calls) == 3  # no pointless sleep after the final attempt
    # Exponential ceilings 1, 2, 4: each jittered delay stays under its ceiling.
    for attempt, delay in enumerate(sleeps.calls):
        assert 0 <= delay <= 2**attempt


def test_network_error_is_retried(sleeps):
    responses = iter([httpx.ConnectError("boom"), {"data": [], "next_cursor": None}])

    def flaky(request: httpx.Request) -> httpx.Response:
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(200, json=item)

    client = CRMClient(
        http=httpx.Client(transport=httpx.MockTransport(flaky), base_url="http://crm"),
        sleep=sleeps,
    )
    assert extract(client, "contacts").raw_count == 0
    assert len(sleeps.calls) == 1


def test_client_error_is_not_retried(sleeps):
    client = CRMClient(
        http=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404, text="nope")),
            base_url="http://crm",
        ),
        sleep=sleeps,
    )
    with pytest.raises(ExtractError, match="404"):
        extract(client, "deals")
    assert sleeps.calls == []


def test_parse_retry_after_forms():
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(format_datetime(now + timedelta(seconds=30)), now=now) == 30.0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None


def test_invalid_records_go_to_rejects_with_reason(make_client):
    client, store = make_client(page_size=50, corrupt_rate=0.1)
    for resource in RESOURCES:
        result = extract(client, resource)
        assert result.rejects, f"expected some corrupt {resource}"
        assert len(result.records) + len(result.rejects) == result.raw_count
        assert all(r.reason and r.resource == resource for r in result.rejects)


def test_reject_reasons_name_the_bad_field():
    good = {
        "id": "d_1", "name": "x", "owner": "o", "stage": "proposal", "amount": 10,
        "currency": "USD", "created_at": "2026-11-02T10:00:00Z",
        "updated_at": "2026-11-02T10:00:00Z",
    }
    bad = [
        {**good, "id": "d_2", "amount": -5},
        {**good, "id": "d_3", "stage": "won??"},
        {**good, "id": "d_4", "stage": "closed_won"},  # closed without close_date
        {**good, "id": "d_5", "updated_at": "2026-11-02T10:00:00"},  # no timezone
    ]
    valid, rejects = validate_records("deals", [good, *bad])
    assert [v.id for v in valid] == ["d_1"]
    reasons = {r.source_id: r.reason for r in rejects}
    assert "amount" in reasons["d_2"]
    assert "stage" in reasons["d_3"]
    assert "close_date" in reasons["d_4"]
    assert "updated_at" in reasons["d_5"]


def test_timestamps_normalised_to_naive_utc():
    deal = Deal.model_validate({
        "id": "d_1", "name": "x", "owner": "o", "stage": "proposal", "amount": "1.50",
        "currency": "USD", "created_at": "2026-11-02T10:00:00+02:00",
        "updated_at": "2026-11-02T10:00:00Z",
    })
    assert deal.created_at == datetime(2026, 11, 2, 8, 0)
    assert deal.created_at.tzinfo is None


def test_dedupe_keeps_latest_version():
    base = {
        "id": "d_1", "name": "x", "owner": "o", "amount": 1, "currency": "USD",
        "created_at": "2026-11-01T00:00:00Z",
    }
    older = Deal.model_validate({**base, "stage": "proposal", "updated_at": "2026-11-02T00:00:00Z"})
    newer = Deal.model_validate(
        {**base, "stage": "negotiation", "updated_at": "2026-11-03T00:00:00Z"}
    )
    assert dedupe_latest([newer, older]) == [newer]
