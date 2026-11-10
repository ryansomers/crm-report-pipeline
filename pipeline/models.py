"""Pydantic models: the contract between the CRM API and the database.

A record that fails validation is not loaded; it becomes a ``Reject`` carrying
the reason, so bad upstream data is visible instead of silently dropped or
silently loaded.

All timestamps are normalised to *naive UTC* before loading. The database
columns are ``timestamp without time zone`` holding UTC; that keeps SQLite (no
timezone type) and PostgreSQL behaving identically in tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

Stage = Literal[
    "prospecting", "qualification", "proposal", "negotiation", "closed_won", "closed_lost"
]
CLOSED_STAGES = {"closed_won", "closed_lost"}
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def to_naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


# AwareDatetime rejects timestamps without an offset: guessing a timezone for
# "2026-11-02T10:00:00" would shift data by hours without anyone noticing.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(to_naive_utc)]


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, frozen=True)

    id: str = Field(min_length=1)
    updated_at: UtcDatetime


class Contact(_Record):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: str = Field(pattern=EMAIL_PATTERN)
    company: str | None = None
    title: str | None = None
    created_at: UtcDatetime


class Deal(_Record):
    contact_id: str | None = None
    name: str
    owner: str = Field(min_length=1)
    stage: Stage
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    created_at: UtcDatetime
    close_date: date | None = None

    @model_validator(mode="after")
    def _closed_deals_have_close_date(self) -> Deal:
        if self.stage in CLOSED_STAGES and self.close_date is None:
            raise ValueError(f"stage {self.stage} requires close_date")
        return self


class Activity(_Record):
    deal_id: str | None = None
    contact_id: str | None = None
    type: Literal["call", "email", "meeting"]
    subject: str = ""
    occurred_at: UtcDatetime


MODELS: dict[str, type[_Record]] = {
    "contacts": Contact,
    "deals": Deal,
    "activities": Activity,
}


@dataclass(frozen=True)
class Reject:
    resource: str
    source_id: str | None
    reason: str
    payload: str  # original record as JSON, for debugging / replay


def _format_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "record"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def validate_records(
    resource: str, raw: list[dict[str, Any]]
) -> tuple[list[_Record], list[Reject]]:
    model = MODELS[resource]
    valid: list[_Record] = []
    rejects: list[Reject] = []
    for record in raw:
        try:
            valid.append(model.model_validate(record))
        except ValidationError as exc:
            source_id = record.get("id") if isinstance(record, dict) else None
            rejects.append(
                Reject(
                    resource=resource,
                    source_id=str(source_id) if source_id not in (None, "") else None,
                    reason=_format_errors(exc),
                    payload=json.dumps(record, default=str, sort_keys=True),
                )
            )
    return valid, rejects


def dedupe_latest(records: list[_Record]) -> list[_Record]:
    """Keep one version per id: the one with the greatest updated_at.

    A record edited while we were paging can be served twice. PostgreSQL
    refuses an INSERT ... ON CONFLICT DO UPDATE that touches the same key twice
    in one statement, so duplicates must be collapsed before loading.
    """
    latest: dict[str, _Record] = {}
    for rec in records:
        current = latest.get(rec.id)
        if current is None or rec.updated_at >= current.updated_at:
            latest[rec.id] = rec
    return list(latest.values())
