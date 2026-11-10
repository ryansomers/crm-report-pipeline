"""Schema and idempotent loading (SQLAlchemy 2.0 Core).

Idempotency comes from ``INSERT ... ON CONFLICT (id) DO UPDATE``: the CRM's own
ids are primary keys, so re-loading a record overwrites it instead of adding a
row. Running the same load twice leaves the tables unchanged.

The upsert carries a guard, ``WHERE stored.updated_at <= incoming.updated_at``,
so an older copy of a record (a late retry, an overlapping backfill) can never
overwrite a newer one.

PostgreSQL and SQLite both implement this syntax, but SQLAlchemy exposes it
through dialect-specific ``insert`` constructs, chosen in ``_dialect_insert``.

Foreign keys are deliberately not enforced between contacts/deals/activities:
the source system does not guarantee referential integrity (a deal can point
at a contact that was rejected or not synced yet), and failing a whole batch
for that would block good data. Relationships are logical (see the README ER
diagram) and checked in SQL when needed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Column,
    Connection,
    Date,
    DateTime,
    Engine,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    func,
    insert,
    select,
    update,
)

from pipeline.extract import CRMClient, extract
from pipeline.models import Reject

log = logging.getLogger(__name__)

metadata = MetaData()


def _lineage_columns() -> list[Column[Any]]:
    # Which run last wrote the row, and when: handy when debugging a bad number.
    return [
        Column("loaded_run_id", Integer, nullable=False),
        Column("loaded_at", DateTime, nullable=False),
    ]


contacts = Table(
    "contacts",
    metadata,
    Column("id", String(64), primary_key=True),  # CRM source id
    Column("first_name", String(100), nullable=False),
    Column("last_name", String(100), nullable=False),
    Column("email", String(255), nullable=False),
    Column("company", String(255)),
    Column("title", String(255)),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    *_lineage_columns(),
)

deals = Table(
    "deals",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("contact_id", String(64)),
    Column("name", String(255), nullable=False),
    Column("owner", String(100), nullable=False),
    Column("stage", String(32), nullable=False),
    # Numeric, never float, for money: 0.1 + 0.2 must equal 0.3.
    Column("amount", Numeric(14, 2), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("close_date", Date),
    Column("updated_at", DateTime, nullable=False),
    *_lineage_columns(),
    Index("ix_deals_created_at", "created_at"),
    Index("ix_deals_close_date", "close_date"),
    Index("ix_deals_stage", "stage"),
)

activities = Table(
    "activities",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("deal_id", String(64)),
    Column("contact_id", String(64)),
    Column("type", String(16), nullable=False),
    Column("subject", Text, nullable=False),
    Column("occurred_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    *_lineage_columns(),
    Index("ix_activities_deal_id", "deal_id"),
    Index("ix_activities_occurred_at", "occurred_at"),
)

pipeline_runs = Table(
    "pipeline_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime),
    Column("status", String(16), nullable=False),  # running | success | failed
    Column("mode", String(16), nullable=False),  # full | incremental
    Column("contacts_loaded", Integer, nullable=False, default=0),
    Column("deals_loaded", Integer, nullable=False, default=0),
    Column("activities_loaded", Integer, nullable=False, default=0),
    Column("rejects", Integer, nullable=False, default=0),
    Column("error", Text),
)

# High-water mark per resource: the largest updated_at successfully loaded.
watermarks = Table(
    "watermarks",
    metadata,
    Column("resource", String(32), primary_key=True),
    Column("high_watermark", DateTime, nullable=False),
    Column("run_id", Integer, nullable=False),
)

rejects = Table(
    "rejects",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", Integer, nullable=False),
    Column("resource", String(32), nullable=False),
    Column("source_id", String(64)),
    Column("reason", Text, nullable=False),
    Column("payload", Text, nullable=False),
    Column("rejected_at", DateTime, nullable=False),
)

TABLES: dict[str, Table] = {"contacts": contacts, "deals": deals, "activities": activities}
RESOURCE_ORDER = ("contacts", "deals", "activities")
BATCH_SIZE = 500  # keeps SQLite under its bound-parameter limit


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)


def _dialect_insert(conn: Connection, table: Table) -> Any:
    name = conn.dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - only the two dialects above are supported
        raise NotImplementedError(f"upsert not implemented for dialect {name!r}")
    return dialect_insert(table)


def upsert(conn: Connection, table: Table, rows: Sequence[dict[str, Any]]) -> int:
    """Insert or update rows keyed on ``id``. Returns rows actually written."""
    written = 0
    pk = [c.name for c in table.primary_key.columns]
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        stmt = _dialect_insert(conn, table).values(list(batch))
        # `excluded` is the row that failed to insert, i.e. the incoming version.
        updatable = {c.name: stmt.excluded[c.name] for c in table.columns if c.name not in pk}
        stmt = stmt.on_conflict_do_update(
            index_elements=pk,
            set_=updatable,
            where=table.c.updated_at <= stmt.excluded.updated_at,
        )
        # Count via RETURNING rather than rowcount: psycopg reports -1 for
        # this statement, and RETURNING omits rows skipped by the WHERE guard.
        written += len(conn.execute(stmt.returning(table.c.id)).all())
    return written


def records_to_rows(table: Table, records: Sequence[Any], run_id: int) -> list[dict[str, Any]]:
    now = utcnow()
    names = {c.name for c in table.columns}
    rows = []
    for rec in records:
        row = {k: v for k, v in rec.model_dump().items() if k in names}
        row["loaded_run_id"] = run_id
        row["loaded_at"] = now
        rows.append(row)
    return rows


def get_watermark(conn: Connection, resource: str) -> datetime | None:
    return conn.execute(
        select(watermarks.c.high_watermark).where(watermarks.c.resource == resource)
    ).scalar_one_or_none()


def set_watermark(conn: Connection, resource: str, value: datetime, run_id: int) -> None:
    stmt = _dialect_insert(conn, watermarks).values(
        resource=resource, high_watermark=value, run_id=run_id
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["resource"],
        set_={"high_watermark": stmt.excluded.high_watermark, "run_id": run_id},
        # Never move the watermark backwards.
        where=watermarks.c.high_watermark < stmt.excluded.high_watermark,
    )
    conn.execute(stmt)


def save_rejects(conn: Connection, run_id: int, items: Sequence[Reject]) -> None:
    if not items:
        return
    now = utcnow()
    conn.execute(
        insert(rejects),
        [
            {
                "run_id": run_id,
                "resource": r.resource,
                "source_id": r.source_id,
                "reason": r.reason,
                "payload": r.payload,
                "rejected_at": now,
            }
            for r in items
        ],
    )


@dataclass
class RunSummary:
    run_id: int
    mode: str
    loaded: dict[str, int] = field(default_factory=dict)
    rejects: dict[str, int] = field(default_factory=dict)
    status: str = "running"


def run_pipeline(engine: Engine, client: CRMClient, full_refresh: bool = False) -> RunSummary:
    """Extract every resource and upsert it. Safe to re-run at any time.

    Each resource is loaded in its own transaction *together with* its new
    watermark. If the job dies halfway, the watermark of an unfinished
    resource has not moved, so the next run re-fetches that data.
    The next incremental run asks for ``updated_since >= watermark``
    (inclusive): records sharing the boundary timestamp are fetched again,
    which the upsert absorbs, rather than risking any being skipped.
    """
    create_schema(engine)
    mode = "full" if full_refresh else "incremental"
    with engine.begin() as conn:
        run_id = conn.execute(
            insert(pipeline_runs).values(started_at=utcnow(), status="running", mode=mode)
        ).inserted_primary_key[0]
    summary = RunSummary(run_id=run_id, mode=mode)

    try:
        for resource in RESOURCE_ORDER:
            with engine.connect() as conn:
                since = None if full_refresh else get_watermark(conn, resource)
            result = extract(client, resource, updated_since=since)
            table = TABLES[resource]
            with engine.begin() as conn:
                rows = records_to_rows(table, result.records, run_id)
                summary.loaded[resource] = upsert(conn, table, rows)
                save_rejects(conn, run_id, result.rejects)
                summary.rejects[resource] = len(result.rejects)
                if result.records:
                    newest = max(r.updated_at for r in result.records)
                    set_watermark(conn, resource, newest, run_id)
    except Exception as exc:
        with engine.begin() as conn:
            conn.execute(
                update(pipeline_runs)
                .where(pipeline_runs.c.id == run_id)
                .values(status="failed", finished_at=utcnow(), error=str(exc)[:2000])
            )
        summary.status = "failed"
        raise

    with engine.begin() as conn:
        conn.execute(
            update(pipeline_runs)
            .where(pipeline_runs.c.id == run_id)
            .values(
                status="success",
                finished_at=utcnow(),
                contacts_loaded=summary.loaded.get("contacts", 0),
                deals_loaded=summary.loaded.get("deals", 0),
                activities_loaded=summary.loaded.get("activities", 0),
                rejects=sum(summary.rejects.values()),
            )
        )
    summary.status = "success"
    log.info("run %s (%s) loaded %s, rejects %s", run_id, mode, summary.loaded, summary.rejects)
    return summary


def row_count(conn: Connection, table: Table) -> int:
    return conn.execute(select(func.count()).select_from(table)).scalar_one()
