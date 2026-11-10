"""Deterministic synthetic CRM data.

Records are stored exactly as the API serves them (JSON-ready dicts with ISO-8601
UTC strings), which keeps the API layer a thin filter-and-page over lists.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from faker import Faker

from mock_crm.config import MockConfig

RESOURCES = ("contacts", "deals", "activities")
OPEN_STAGES = ("prospecting", "qualification", "proposal", "negotiation")
CLOSED_STAGES = ("closed_won", "closed_lost")
ACTIVITY_TYPES = ("call", "email", "meeting")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

Record = dict[str, Any]


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=UTC)


class CRMStore:
    """In-memory CRM. Same config -> same records, every time."""

    def __init__(self, config: MockConfig | None = None) -> None:
        self.config = config or MockConfig()
        # Separate RNG streams so that, e.g., changing corrupt_rate does not
        # reshuffle the clean data.
        self._rng = random.Random(self.config.seed)
        self._fake = Faker()
        self._fake.seed_instance(self.config.seed)
        # Logical clock for simulated edits; always moves forward.
        self._clock = self.config.data_end
        self.records: dict[str, list[Record]] = {r: [] for r in RESOURCES}
        self._generate()
        if self.config.corrupt_rate > 0:
            self._corrupt(random.Random(self.config.seed + 1))

    # ------------------------------------------------------------------ build
    def _random_between(self, start: datetime, end: datetime) -> datetime:
        seconds = int((end - start).total_seconds())
        return start + timedelta(seconds=self._rng.randint(0, max(seconds, 0)))

    def _generate(self) -> None:
        cfg = self.config
        fake, rng = self._fake, self._rng
        start = cfg.data_end - timedelta(weeks=cfg.history_weeks)
        owners = [fake.name() for _ in range(6)]

        for i in range(1, cfg.n_contacts + 1):
            first, last = fake.first_name(), fake.last_name()
            company = fake.company()
            created = self._random_between(start - timedelta(weeks=8), cfg.data_end)
            updated = self._random_between(created, cfg.data_end)
            domain = fake.domain_name()
            self.records["contacts"].append(
                {
                    "id": f"c_{i:05d}",
                    "first_name": first,
                    "last_name": last,
                    "email": f"{first}.{last}@{domain}".lower(),
                    "company": company,
                    "title": fake.job(),
                    "created_at": fmt_ts(created),
                    "updated_at": fmt_ts(updated),
                }
            )

        contacts = self.records["contacts"]
        for i in range(1, cfg.n_deals + 1):
            contact = rng.choice(contacts)
            created = self._random_between(start, cfg.data_end)
            age_days = (cfg.data_end - created).days
            # Older deals are more likely to have closed.
            p_closed = min(0.85, age_days / 40)
            close_date = None
            if age_days >= 3 and rng.random() < p_closed:
                stage = "closed_won" if rng.random() < 0.45 else "closed_lost"
                closed_at = created + timedelta(days=rng.randint(3, min(age_days, 45)))
                close_date = closed_at.date().isoformat()
                updated = closed_at
            else:
                # Further along the funnel the longer the deal has existed.
                idx = min(len(OPEN_STAGES) - 1, int(age_days / 10) + rng.randint(-1, 1))
                stage = OPEN_STAGES[max(0, idx)]
                updated = self._random_between(created, cfg.data_end)
            amount = round(rng.lognormvariate(9.6, 0.7), 2)  # median ~ $15k
            self.records["deals"].append(
                {
                    "id": f"d_{i:05d}",
                    "contact_id": contact["id"],
                    "name": f"{contact['company']} - {fake.bs().title()}"[:120],
                    "owner": rng.choice(owners),
                    "stage": stage,
                    "amount": amount,
                    "currency": "USD",
                    "created_at": fmt_ts(created),
                    "close_date": close_date,
                    "updated_at": fmt_ts(min(updated, cfg.data_end)),
                }
            )

        deals = self.records["deals"]
        for i in range(1, cfg.n_activities + 1):
            deal = rng.choice(deals)
            occurred = self._random_between(parse_ts(deal["created_at"]), cfg.data_end)
            self.records["activities"].append(
                {
                    "id": f"a_{i:05d}",
                    "deal_id": deal["id"],
                    "contact_id": deal["contact_id"],
                    "type": rng.choice(ACTIVITY_TYPES),
                    "subject": fake.sentence(nb_words=5).rstrip("."),
                    "occurred_at": fmt_ts(occurred),
                    "updated_at": fmt_ts(occurred),
                }
            )

    def _corrupt(self, rng: random.Random) -> None:
        """Inject realistic defects so the pipeline's reject path has work to do."""
        defects = {
            "contacts": [("email", "not-an-email"), ("id", "")],
            "deals": [("amount", -500.0), ("stage", "won??"), ("amount", "TBD")],
            "activities": [("type", "fax"), ("occurred_at", None)],
        }
        for resource, options in defects.items():
            for record in self.records[resource]:
                if rng.random() < self.config.corrupt_rate:
                    field, bad_value = rng.choice(options)
                    record[field] = bad_value

    # ---------------------------------------------------------------- mutate
    def simulate_changes(self, n: int) -> list[str]:
        """Advance ``n`` open deals one stage (or close them). Returns changed ids.

        Each change gets a strictly later ``updated_at``, which is what an
        incremental (watermark) load relies on.
        """
        open_deals = [d for d in self.records["deals"] if d["stage"] in OPEN_STAGES]
        changed: list[str] = []
        for deal in self._rng.sample(open_deals, min(n, len(open_deals))):
            self._clock += timedelta(minutes=1)
            idx = OPEN_STAGES.index(deal["stage"])
            if idx == len(OPEN_STAGES) - 1:
                deal["stage"] = self._rng.choice(CLOSED_STAGES)
                deal["close_date"] = self._clock.date().isoformat()
            else:
                deal["stage"] = OPEN_STAGES[idx + 1]
            deal["updated_at"] = fmt_ts(self._clock)
            changed.append(deal["id"])
        return changed

    # ----------------------------------------------------------------- query
    def sorted_records(self, resource: str, updated_since: str | None = None) -> list[Record]:
        """Records ordered by (updated_at, id): the keyset the cursor walks.

        ``updated_since`` is inclusive. Fixed-width ISO strings sort the same
        as the instants they encode, so plain string comparison is correct.
        """
        rows = self.records[resource]
        if updated_since is not None:
            rows = [r for r in rows if (r.get("updated_at") or "") >= updated_since]
        return sorted(rows, key=lambda r: (r.get("updated_at") or "", str(r.get("id"))))
