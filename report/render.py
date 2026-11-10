"""Render the report as Markdown and as a small standalone HTML page.

Both renderers read the same list of ``Table`` objects, so the two formats
cannot drift apart.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from report.summarize import SummaryOutcome, money


@dataclass
class Table:
    title: str
    headers: list[str]
    rows: list[list[str]]


def _num(v: Any) -> str:
    return "n/a" if v is None else f"{v:,}" if isinstance(v, int) else f"{v:,.2f}"


def _delta(entry: dict[str, Any], is_money: bool) -> str:
    if entry["delta"] is None:
        return "n/a"
    sign = "+" if entry["delta"] > 0 else "-" if entry["delta"] < 0 else ""
    amount = money(abs(entry["delta"])) if is_money else _num(abs(entry["delta"]))
    pct = f" ({sign}{abs(entry['delta_pct'])}%)" if entry["delta_pct"] is not None else ""
    return f"{sign}{amount}{pct}"


def build_tables(m: dict[str, Any]) -> list[Table]:
    wow = m["week_over_week"]
    fmt = {True: money, False: _num}
    kpi_rows = [
        [label, fmt[is_money](wow[key]["current"]), fmt[is_money](wow[key]["previous"]),
         _delta(wow[key], is_money)]
        for label, key, is_money in [
            ("New deals", "new_deals", False),
            ("New deal value", "new_value", True),
            ("Deals won", "won_count", False),
            ("Won value", "won_value", True),
            ("Deals lost", "lost_count", False),
        ]
    ]
    win_rate = m["closed"]["win_rate_pct"]
    kpi_rows.append(["Win rate", "n/a" if win_rate is None else f"{win_rate}%", "", ""])
    pipeline = m["open_pipeline"]
    return [
        Table("Key metrics", ["Metric", "This week", "Last week", "Change"], kpi_rows),
        Table(
            "Open pipeline by stage",
            ["Stage", "Deals", "Value"],
            [[s["stage"], _num(s["deal_count"]), money(s["value"])] for s in pipeline["by_stage"]]
            + [["**Total**", _num(pipeline["deal_count"]), money(pipeline["value"])]],
        ),
        Table(
            "Top owners by won value",
            ["Rank", "Owner", "Won deals", "Won value"],
            [[str(o["rank"]), o["owner"], _num(o["won_count"]), money(o["won_value"])]
             for o in m["top_owners"]],
        ),
        Table(
            f"Trend (last {len(m['weekly_trend'])} weeks)",
            ["Week", "New deals", "Won", "Won value", "Lost"],
            [[w["week_start"], _num(w["new_deals"]), _num(w["won_count"]),
              money(w["won_value"]), _num(w["lost_count"])] for w in m["weekly_trend"]],
        ),
    ]


def _provenance(outcome: SummaryOutcome) -> str:
    check = "passed" if outcome.verification.ok else "FAILED"
    text = (
        f"Summary source: {outcome.source}. Figure check: {check} "
        f"({outcome.verification.checked} figures checked)."
    )
    if outcome.notes:
        text += " Notes: " + "; ".join(outcome.notes) + "."
    return text


def render_markdown(m: dict[str, Any], outcome: SummaryOutcome) -> str:
    s = outcome.summary
    out = [
        f"# Weekly Sales Report: {m['week_start']} to {m['week_end']}",
        "",
        f"_{_provenance(outcome)}_",
        "",
        "## Summary",
        "",
        f"**{s.headline}**",
        "",
        *[f"- {h}" for h in s.highlights],
    ]
    if s.watch_items:
        out += ["", "**Watch:**", "", *[f"- {w}" for w in s.watch_items]]
    for t in build_tables(m):
        out += ["", f"## {t.title}", ""]
        if not t.rows:
            out.append("_None this week._")
            continue
        out.append("| " + " | ".join(t.headers) + " |")
        out.append("|" + "|".join("---" for _ in t.headers) + "|")
        out += ["| " + " | ".join(r) + " |" for r in t.rows]
    return "\n".join(out) + "\n"


CSS = """
body{font-family:system-ui,-apple-system,sans-serif;max-width:860px;margin:2rem auto;
padding:0 1rem;color:#1f2328;line-height:1.5}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem}
.meta{color:#59636e;font-size:.85rem}
table{border-collapse:collapse;width:100%}
th,td{padding:.35rem .6rem;border-bottom:1px solid #d1d9e0}
th{text-align:left;background:#f6f8fa}td:not(:first-child){font-variant-numeric:tabular-nums}
"""


def render_html(m: dict[str, Any], outcome: SummaryOutcome) -> str:
    e = html.escape
    s = outcome.summary
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Weekly Sales Report {e(m['week_start'])}</title><style>{CSS}</style></head>",
        f"<body><h1>Weekly Sales Report: {e(m['week_start'])} to {e(m['week_end'])}</h1>",
        f"<p class='meta'>{e(_provenance(outcome))}</p>",
        f"<h2>Summary</h2><p><strong>{e(s.headline)}</strong></p>",
        "<ul>" + "".join(f"<li>{e(h)}</li>" for h in s.highlights) + "</ul>",
    ]
    if s.watch_items:
        parts.append("<p><strong>Watch:</strong></p><ul>"
                     + "".join(f"<li>{e(w)}</li>" for w in s.watch_items) + "</ul>")
    for t in build_tables(m):
        parts.append(f"<h2>{e(t.title)}</h2>")
        if not t.rows:
            parts.append("<p><em>None this week.</em></p>")
            continue
        head = "".join(f"<th>{e(h)}</th>" for h in t.headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{e(c.replace('**', ''))}</td>" for c in r) + "</tr>"
            for r in t.rows
        )
        parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def write_report(
    m: dict[str, Any], outcome: SummaryOutcome, out_dir: Path, week: date
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"weekly-{week.isoformat()}"
    paths = {
        "markdown": out_dir / f"{stem}.md",
        "html": out_dir / f"{stem}.html",
        # The exact numbers the summary was checked against, for auditing.
        "metrics": out_dir / f"{stem}.metrics.json",
    }
    paths["markdown"].write_text(render_markdown(m, outcome))
    paths["html"].write_text(render_html(m, outcome))
    paths["metrics"].write_text(json.dumps(m, indent=2) + "\n")
    return paths
