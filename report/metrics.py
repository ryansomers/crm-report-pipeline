"""Run the weekly SQL and assemble one JSON-ready metrics dict.

The SQL lives in ``report/sql/*.sql`` so it can be read, reviewed and run on
its own (psql, sqlite3). Queries in ``sql/`` are portable; queries that need
dialect features live in ``sql/postgres/`` and ``sql/sqlite/`` with the same
name and the same output columns.

Every number the LLM is allowed to mention comes out of this module.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Date, DateTime, Engine, Integer, bindparam, text

SQL_DIR = Path(__file__).parent / "sql"
DIALECT_SPECIFIC = {"weekly_trend"}

# Bind each parameter with an explicit type so SQLAlchemy renders it in the
# format each backend stores (SQLite compares dates/timestamps as strings).
PARAM_TYPES = {
    "week_start": DateTime,
    "week_end": DateTime,
    "first_week": DateTime,
    "week_start_date": Date,
    "week_end_date": Date,
    "first_week_date": Date,
    "top_n": Integer,
}


def load_sql(name: str, dialect: str) -> str:
    if name in DIALECT_SPECIFIC:
        if dialect not in ("postgresql", "sqlite"):
            raise NotImplementedError(f"no {name}.sql for dialect {dialect!r}")
        folder = "postgres" if dialect == "postgresql" else "sqlite"
        return (SQL_DIR / folder / f"{name}.sql").read_text()
    return (SQL_DIR / f"{name}.sql").read_text()


def run_query(engine: Engine, name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    sql = load_sql(name, engine.dialect.name)
    used = set(re.findall(r"(?<!:):(\w+)", sql))  # skip PostgreSQL '::' casts
    stmt = text(sql).bindparams(
        *(bindparam(p, type_=PARAM_TYPES[p]) for p in sorted(used) if p in PARAM_TYPES)
    )
    with engine.connect() as conn:
        result = conn.execute(stmt, {k: v for k, v in params.items() if k in used})
        return [dict(row) for row in result.mappings()]


def _num(value: Any, places: int = 2) -> float | int | None:
    """Normalise driver output: PostgreSQL gives Decimal, SQLite gives float."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return round(float(Decimal(str(value))), places)


def _pct_change(current: float, previous: float | None) -> float | None:
    if not previous:
        return None  # undefined when last week was zero
    return round(100.0 * (current - previous) / previous, 1)


def week_bounds(week_start: date) -> tuple[datetime, datetime]:
    start = datetime.combine(week_start, time.min)
    return start, start + timedelta(days=7)


def compute_metrics(
    engine: Engine, week_start: date, trend_weeks: int = 6, top_n: int = 3
) -> dict[str, Any]:
    if week_start.weekday() != 0:
        raise ValueError(f"week must start on a Monday, got {week_start} ({week_start:%A})")
    start, end = week_bounds(week_start)
    first_week = week_start - timedelta(weeks=trend_weeks - 1)
    params = {
        "week_start": start,
        "week_end": end,
        "week_start_date": week_start,
        "week_end_date": week_start + timedelta(days=7),
        "first_week": datetime.combine(first_week, time.min),
        "first_week_date": first_week,
        "top_n": top_n,
    }

    new = run_query(engine, "new_deals", params)[0]
    closed = run_query(engine, "closed_deals", params)[0]
    stages = run_query(engine, "pipeline_by_stage", params)
    owners = run_query(engine, "top_owners", params)
    trend = run_query(engine, "weekly_trend", params)

    current = trend[-1]
    wow = {}
    for key in ("new_deals", "new_value", "won_count", "won_value", "lost_count"):
        cur = _num(current[key]) or 0
        prev = _num(current[f"prev_{key}"])
        wow[key] = {
            "current": cur,
            "previous": prev,
            "delta": None if prev is None else _num(cur - prev),
            "delta_pct": _pct_change(cur, prev),
        }

    by_stage = [
        {"stage": r["stage"], "deal_count": r["deal_count"], "value": _num(r["total_value"])}
        for r in stages
    ]
    return {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
        "currency": "USD",
        "new_deals": {"count": new["deal_count"], "value": _num(new["total_value"])},
        "closed": {
            "won_count": _num(closed["won_count"]) or 0,
            "won_value": _num(closed["won_value"]) or 0,
            "lost_count": _num(closed["lost_count"]) or 0,
            "lost_value": _num(closed["lost_value"]) or 0,
            "win_rate_pct": _num(closed["win_rate_pct"], 1),
        },
        "open_pipeline": {
            "deal_count": sum(s["deal_count"] for s in by_stage),
            "value": _num(sum(s["value"] or 0 for s in by_stage)),
            "by_stage": by_stage,
        },
        "top_owners": [
            {
                "rank": r["owner_rank"],
                "owner": r["owner"],
                "won_count": r["won_count"],
                "won_value": _num(r["won_value"]),
            }
            for r in owners
        ],
        "week_over_week": wow,
        "weekly_trend": [
            {
                "week_start": str(r["week_start"]),
                "new_deals": _num(r["new_deals"]),
                "won_count": _num(r["won_count"]),
                "won_value": _num(r["won_value"]),
                "lost_count": _num(r["lost_count"]),
            }
            for r in trend
        ],
    }
