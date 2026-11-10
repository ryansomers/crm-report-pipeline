"""Command line entry point: ``crm-pipeline <command>``.

    crm-pipeline serve-mock [--port 8000] [--rate-limit-every 7] [--fail-every 11]
    crm-pipeline run --db-url URL [--base-url URL | --in-process-mock] [--full-refresh]
    crm-pipeline report --db-url URL --week 2026-11-02 [--no-llm]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import httpx
from sqlalchemy import create_engine

DEFAULT_DB = "sqlite:///crm.db"


def _add_db_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--db-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DB),
        help="SQLAlchemy URL (default: $DATABASE_URL or %(default)s)",
    )


def cmd_serve_mock(args: argparse.Namespace) -> int:
    import uvicorn

    from mock_crm import MockConfig, create_app

    config = MockConfig(
        seed=args.seed,
        rate_limit_every=args.rate_limit_every,
        fail_every=args.fail_every,
        corrupt_rate=args.corrupt_rate,
    )
    uvicorn.run(create_app(config), host=args.host, port=args.port)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from pipeline.extract import CRMClient, ExtractError
    from pipeline.load import run_pipeline

    if args.in_process_mock:
        # Serve the mock CRM inside this process: no server, no network.
        from fastapi.testclient import TestClient

        from mock_crm import MockConfig, create_app

        http: httpx.Client = TestClient(
            create_app(MockConfig(rate_limit_every=7, fail_every=11, corrupt_rate=0.02))
        )
    else:
        http = httpx.Client(base_url=args.base_url, timeout=10.0)

    engine = create_engine(args.db_url)
    client = CRMClient(http=http, page_size=args.page_size)
    try:
        summary = run_pipeline(engine, client, full_refresh=args.full_refresh)
    except ExtractError as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 1
    finally:
        http.close()
    print(
        f"run {summary.run_id} ({summary.mode}): loaded {summary.loaded}, "
        f"rejected {summary.rejects}, {client.request_count} HTTP requests"
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from pipeline.load import create_schema
    from report.metrics import compute_metrics
    from report.render import write_report
    from report.summarize import AnthropicSummarizer, TemplateSummarizer, generate_summary

    engine = create_engine(args.db_url)
    create_schema(engine)
    try:
        metrics = compute_metrics(engine, args.week, trend_weeks=args.trend_weeks)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    summarizer = TemplateSummarizer() if args.no_llm else AnthropicSummarizer()
    outcome = generate_summary(summarizer, metrics)
    paths = write_report(metrics, outcome, Path(args.out_dir), args.week)
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    if outcome.notes:
        print("notes: " + "; ".join(outcome.notes), file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crm-pipeline", description=__doc__.split("\n")[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve-mock", help="run the mock CRM API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--rate-limit-every", type=int, default=None,
                   help="every Nth request gets 429 + Retry-After")
    p.add_argument("--fail-every", type=int, default=None, help="every Nth request gets 503")
    p.add_argument("--corrupt-rate", type=float, default=0.0,
                   help="fraction of records with deliberate data errors")
    p.set_defaults(func=cmd_serve_mock)

    p = sub.add_parser("run", help="extract from the CRM and upsert into the database")
    _add_db_arg(p)
    p.add_argument("--base-url", default=os.environ.get("CRM_BASE_URL", "http://localhost:8000"))
    p.add_argument("--in-process-mock", action="store_true",
                   help="use an in-process mock CRM (with injected 429s/503s/bad records)")
    p.add_argument("--full-refresh", action="store_true", help="ignore watermarks")
    p.add_argument("--page-size", type=int, default=100)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="compute weekly metrics and write the report")
    _add_db_arg(p)
    p.add_argument("--week", type=date.fromisoformat, required=True,
                   help="Monday that starts the report week, e.g. 2026-11-02")
    p.add_argument("--no-llm", action="store_true", help="template summary, no API key needed")
    p.add_argument("--out-dir", default="reports")
    p.add_argument("--trend-weeks", type=int, default=6)
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
