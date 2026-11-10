from __future__ import annotations

import random
from datetime import datetime

import httpx
import pytest
from sqlalchemy import create_engine, select

from pipeline.extract import CRMClient, ExtractError, RetryPolicy
from pipeline.load import (
    contacts,
    create_schema,
    deals,
    pipeline_runs,
    rejects,
    row_count,
    run_pipeline,
    upsert,
    watermarks,
)
from pipeline.models import Deal


@pytest.fixture
def engine():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(eng)
    return eng


def snapshot(engine, table):
    """Table contents without lineage columns (those change on every write)."""
    with engine.connect() as conn:
        rows = conn.execute(select(table).order_by(table.c.id)).mappings().all()
    return [{k: v for k, v in r.items() if not k.startswith("loaded_")} for r in rows]


def test_full_load_twice_does_not_duplicate(engine, make_client):
    client, store = make_client(page_size=40)
    first = run_pipeline(engine, client, full_refresh=True)
    before = snapshot(engine, deals)

    second = run_pipeline(engine, client, full_refresh=True)
    after = snapshot(engine, deals)

    assert first.status == second.status == "success"
    assert len(before) == len(store.records["deals"])
    assert before == after
    with engine.connect() as conn:
        assert row_count(conn, contacts) == len(store.records["contacts"])
        runs = conn.execute(select(pipeline_runs.c.status)).scalars().all()
    assert runs == ["success", "success"]


def test_incremental_run_updates_changed_rows_only(engine, make_client):
    client, store = make_client(page_size=40)
    run_pipeline(engine, client)  # no watermark yet -> loads everything
    changed = store.simulate_changes(5)
    new_stage = {d["id"]: d["stage"] for d in store.records["deals"] if d["id"] in changed}

    summary = run_pipeline(engine, client)

    # Changed deals plus any re-read at the inclusive watermark boundary.
    assert 5 <= summary.loaded["deals"] <= 7
    assert summary.loaded["contacts"] <= 2
    with engine.connect() as conn:
        assert row_count(conn, deals) == len(store.records["deals"])
        stored = dict(
            conn.execute(select(deals.c.id, deals.c.stage).where(deals.c.id.in_(changed))).all()
        )
        wm = conn.execute(
            select(watermarks.c.high_watermark).where(watermarks.c.resource == "deals")
        ).scalar_one()
    assert stored == new_stage
    assert wm > store.config.data_end.replace(tzinfo=None)


def _deal(stage: str, updated: str) -> Deal:
    return Deal.model_validate({
        "id": "d_1", "name": "x", "owner": "o", "stage": stage, "amount": 1,
        "currency": "USD", "created_at": "2026-11-01T00:00:00Z", "updated_at": updated,
    })


def _row(deal: Deal) -> dict:
    return {**deal.model_dump(), "loaded_run_id": 1, "loaded_at": datetime(2026, 11, 10)}


def test_older_version_never_overwrites_newer(engine):
    newer = _deal("negotiation", "2026-11-05T00:00:00Z")
    older = _deal("proposal", "2026-11-03T00:00:00Z")
    with engine.begin() as conn:
        assert upsert(conn, deals, [_row(newer)]) == 1
        assert upsert(conn, deals, [_row(older)]) == 0  # guarded out
        stage = conn.execute(select(deals.c.stage)).scalar_one()
    assert stage == "negotiation"


def test_rejects_are_persisted_with_reason(engine, make_client):
    client, store = make_client(page_size=50, corrupt_rate=0.1)
    summary = run_pipeline(engine, client, full_refresh=True)
    with engine.connect() as conn:
        rows = conn.execute(select(rejects)).mappings().all()
        loaded_deals = row_count(conn, deals)
        run = conn.execute(select(pipeline_runs)).mappings().one()
    assert len(rows) == sum(summary.rejects.values()) > 0
    assert all(r["reason"] and r["payload"] for r in rows)
    assert loaded_deals + summary.rejects["deals"] == len(store.records["deals"])
    assert run["rejects"] == len(rows)


def test_failed_run_is_recorded_and_watermark_unchanged(engine, make_client, sleeps):
    good_client, _ = make_client(page_size=50)
    run_pipeline(engine, good_client)
    with engine.connect() as conn:
        wm_before = conn.execute(select(watermarks)).all()

    broken = CRMClient(
        http=httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(503)), base_url="http://crm"
        ),
        retry=RetryPolicy(max_attempts=2),
        sleep=sleeps,
        rng=random.Random(0),
    )
    with pytest.raises(ExtractError):
        run_pipeline(engine, broken)

    with engine.connect() as conn:
        last = conn.execute(
            select(pipeline_runs).order_by(pipeline_runs.c.id.desc())
        ).mappings().first()
        wm_after = conn.execute(select(watermarks)).all()
    assert last["status"] == "failed" and "503" in last["error"]
    assert wm_after == wm_before


@pytest.mark.integration
def test_postgres_upsert_is_idempotent(make_client, pg_engine):
    client, store = make_client(page_size=100)
    run_pipeline(pg_engine, client, full_refresh=True)
    before = snapshot(pg_engine, deals)
    run_pipeline(pg_engine, client, full_refresh=True)
    assert snapshot(pg_engine, deals) == before
    assert len(before) == len(store.records["deals"])


@pytest.mark.integration
def test_postgres_upsert_counts_written_rows(pg_engine):
    newer = _deal("negotiation", "2026-11-05T00:00:00Z")
    older = _deal("proposal", "2026-11-03T00:00:00Z")
    with pg_engine.begin() as conn:
        assert upsert(conn, deals, [_row(newer)]) == 1
        assert upsert(conn, deals, [_row(older)]) == 0
        assert upsert(conn, deals, [_row(newer)]) == 1
