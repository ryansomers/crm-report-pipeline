"""FastAPI app exposing the fake CRM with keyset (cursor) pagination.

Response shape for every list endpoint::

    {"data": [...], "next_cursor": "opaque-string-or-null"}

Why a keyset cursor instead of ?page=N: offset paging skips or repeats rows
when records change between page requests. A cursor that encodes the last
(updated_at, id) seen resumes exactly after that row, whatever happens to rows
before it. A record edited mid-scan moves past the cursor and may be served
twice; the loader's upsert makes that harmless.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from mock_crm.config import MockConfig
from mock_crm.data import RESOURCES, CRMStore, fmt_ts


def encode_cursor(updated_at: str, record_id: str) -> str:
    raw = json.dumps([updated_at, record_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        updated_at, record_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return str(updated_at), str(record_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc


def create_app(config: MockConfig | None = None, store: CRMStore | None = None) -> FastAPI:
    config = config or (store.config if store else MockConfig())
    store = store or CRMStore(config)
    app = FastAPI(title="Mock CRM", version="1.0")
    app.state.store = store
    app.state.request_count = 0

    @app.middleware("http")
    async def inject_failures(request: Request, call_next: Any) -> Any:
        """Simulate an unreliable upstream: periodic 429s and 503s."""
        if request.url.path.lstrip("/") in RESOURCES:
            app.state.request_count += 1
            n = app.state.request_count
            if config.rate_limit_every and n % config.rate_limit_every == 0:
                return JSONResponse(
                    {"error": "rate_limited"},
                    status_code=429,
                    headers={"Retry-After": str(config.retry_after_seconds)},
                )
            if config.fail_every and n % config.fail_every == 0:
                return JSONResponse({"error": "upstream unavailable"}, status_code=503)
        return await call_next(request)

    def list_resource(
        resource: str, limit: int | None, cursor: str | None, updated_since: datetime | None
    ) -> dict[str, Any]:
        page_size = min(limit or config.default_page_size, config.max_page_size)
        since = fmt_ts(updated_since) if updated_since else None
        rows = store.sorted_records(resource, since)
        if cursor:
            after = decode_cursor(cursor)
            rows = [r for r in rows if (r.get("updated_at") or "", str(r.get("id"))) > after]
        page = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = page[-1]
            next_cursor = encode_cursor(last.get("updated_at") or "", str(last.get("id")))
        return {"data": page, "next_cursor": next_cursor}

    def make_endpoint(resource: str) -> Any:
        # A factory (not a default argument) binds the loop variable without
        # FastAPI exposing it as a query parameter.
        def endpoint(
            limit: Annotated[int | None, Query(ge=1)] = None,
            cursor: str | None = None,
            updated_since: datetime | None = None,
        ) -> dict[str, Any]:
            return list_resource(resource, limit, cursor, updated_since)

        return endpoint

    for resource in RESOURCES:
        app.add_api_route(
            f"/{resource}", make_endpoint(resource), methods=["GET"], name=f"list_{resource}"
        )

    @app.post("/_admin/simulate-changes")
    def simulate_changes(n: int = 10) -> dict[str, Any]:
        """Demo helper: edit some deals so the next incremental run has work."""
        return {"changed": store.simulate_changes(n)}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
