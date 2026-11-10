"""Metrics SQL against a hand-built fixture whose answers were worked out by hand.

Report week: Mon 2026-11-02 .. Sun 2026-11-08. Previous week: 2026-10-26 .. 11-01.
The fixture deliberately puts deals on the boundaries (Sunday closes, a deal
created at exactly 2026-11-09 00:00) to catch off-by-one window errors.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine

from pipeline.load import create_schema, deals, upsert
from report.metrics import compute_metrics

WEEK = date(2026, 11, 2)

# id, owner, stage, amount, created_at, close_date
FIXTURE = [
    ("d1", "Alice", "closed_won", "1000.00", "2026-11-02 09:00", "2026-11-03"),
    ("d2", "Alice", "closed_won", "500.50", "2026-10-20 12:00", "2026-11-05"),
    ("d3", "Bob", "closed_won", "2000.00", "2026-10-15 12:00", "2026-11-08"),  # Sunday
    ("d4", "Bob", "closed_lost", "300.00", "2026-11-04 12:00", "2026-11-06"),
    ("d5", "Cara", "proposal", "750.25", "2026-11-08 23:59", None),
    ("d6", "Cara", "prospecting", "100.00", "2026-11-09 00:00", None),  # next week
    ("d7", "Dan", "negotiation", "4000.00", "2026-10-28 12:00", None),
    ("d8", "Dan", "closed_won", "1500.00", "2026-10-01 12:00", "2026-10-27"),
    ("d9", "Alice", "closed_lost", "200.00", "2026-10-26 00:00", "2026-10-30"),
    ("d10", "Bob", "closed_lost", "50.00", "2026-10-10 12:00", "2026-11-01"),  # Sunday
    ("d11", "Cara", "qualification", "600.00", "2026-10-29 12:00", None),
]


def load_fixture(engine) -> None:
    rows = []
    for id_, owner, stage, amount, created, closed in FIXTURE:
        created_at = datetime.fromisoformat(created)
        rows.append({
            "id": id_, "contact_id": None, "name": f"Deal {id_}", "owner": owner,
            "stage": stage, "amount": amount, "currency": "USD", "created_at": created_at,
            "close_date": date.fromisoformat(closed) if closed else None,
            "updated_at": created_at, "loaded_run_id": 1, "loaded_at": created_at,
        })
    with engine.begin() as conn:
        upsert(conn, deals, rows)


EXPECTED = {
    "week_start": "2026-11-02",
    "week_end": "2026-11-08",
    "currency": "USD",
    "new_deals": {"count": 3, "value": 2050.25},  # d1, d4, d5 (not d6)
    "closed": {
        "won_count": 3, "won_value": 3500.5, "lost_count": 1, "lost_value": 300.0,
        "win_rate_pct": 75.0,
    },
    "open_pipeline": {
        "deal_count": 3,
        "value": 5350.25,
        "by_stage": [
            {"stage": "qualification", "deal_count": 1, "value": 600.0},
            {"stage": "proposal", "deal_count": 1, "value": 750.25},
            {"stage": "negotiation", "deal_count": 1, "value": 4000.0},
        ],
    },
    "top_owners": [
        {"rank": 1, "owner": "Bob", "won_count": 1, "won_value": 2000.0},
        {"rank": 2, "owner": "Alice", "won_count": 2, "won_value": 1500.5},
    ],
    "week_over_week": {
        "new_deals": {"current": 3, "previous": 3, "delta": 0, "delta_pct": 0.0},
        "new_value": {
            "current": 2050.25, "previous": 4800.0, "delta": -2749.75, "delta_pct": -57.3,
        },
        "won_count": {"current": 3, "previous": 1, "delta": 2, "delta_pct": 200.0},
        "won_value": {
            "current": 3500.5, "previous": 1500.0, "delta": 2000.5, "delta_pct": 133.4,
        },
        "lost_count": {"current": 1, "previous": 2, "delta": -1, "delta_pct": -50.0},
    },
    "weekly_trend": [
        {"week_start": "2026-10-19", "new_deals": 1, "won_count": 0, "won_value": 0,
         "lost_count": 0},
        {"week_start": "2026-10-26", "new_deals": 3, "won_count": 1, "won_value": 1500.0,
         "lost_count": 2},
        {"week_start": "2026-11-02", "new_deals": 3, "won_count": 3, "won_value": 3500.5,
         "lost_count": 1},
    ],
}


@pytest.fixture
def fixture_engine():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(eng)
    load_fixture(eng)
    return eng


def test_weekly_metrics_match_hand_computed_values(fixture_engine):
    assert compute_metrics(fixture_engine, WEEK, trend_weeks=3) == EXPECTED


def test_empty_week_has_null_win_rate_and_zero_counts(fixture_engine):
    m = compute_metrics(fixture_engine, date(2026, 6, 1), trend_weeks=2)
    assert m["new_deals"] == {"count": 0, "value": 0}
    assert m["closed"]["win_rate_pct"] is None
    assert m["top_owners"] == []
    # Previous week also empty: delta 0, percentage change undefined.
    assert m["week_over_week"]["won_count"] == {
        "current": 0, "previous": 0, "delta": 0, "delta_pct": None
    }


def test_trend_spine_includes_weeks_without_deals(fixture_engine):
    # The fixture has no deals before 2026-10-01; those weeks must still be rows.
    m = compute_metrics(fixture_engine, WEEK, trend_weeks=8)
    weeks = [w["week_start"] for w in m["weekly_trend"]]
    assert len(weeks) == 8 and weeks[0] == "2026-09-14" and weeks[-1] == "2026-11-02"


def test_rank_ties_share_a_rank(fixture_engine):
    tie = {
        "id": "d12", "contact_id": None, "name": "tie", "owner": "Erin", "stage": "closed_won",
        "amount": "2000.00", "currency": "USD", "created_at": datetime(2026, 11, 1),
        "close_date": date(2026, 11, 4), "updated_at": datetime(2026, 11, 4),
        "loaded_run_id": 1, "loaded_at": datetime(2026, 11, 4),
    }
    with fixture_engine.begin() as conn:
        upsert(conn, deals, [tie])
    owners = compute_metrics(fixture_engine, WEEK, top_n=3)["top_owners"]
    assert [(o["rank"], o["owner"]) for o in owners] == [(1, "Bob"), (1, "Erin"), (3, "Alice")]


def test_rejects_non_monday(fixture_engine):
    with pytest.raises(ValueError, match="Monday"):
        compute_metrics(fixture_engine, date(2026, 11, 3))


@pytest.mark.integration
def test_postgres_sql_matches_sqlite_expectations(pg_engine):
    """Runs the PostgreSQL-specific weekly_trend.sql against the same fixture."""
    load_fixture(pg_engine)
    assert compute_metrics(pg_engine, WEEK, trend_weeks=3) == EXPECTED
