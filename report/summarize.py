"""Turn the metrics JSON into a short written summary.

Two implementations share one small interface (``Summarizer``):

* ``TemplateSummarizer``: deterministic f-strings. No key needed; used by
  ``--no-llm`` and as the fallback when an LLM draft fails verification.
* ``AnthropicSummarizer``: Claude via the Anthropic SDK, with structured output
  so the response is a validated ``WeeklySummary`` rather than free text.

Reliability comes from three layers, not from trusting the prompt:
1. The model sees only the computed metrics, never raw rows, so it has no
   material to compute its own (possibly wrong) aggregates from.
2. The prompt forbids new figures and arithmetic.
3. ``generate_summary`` verifies every figure (``report.verify``), gives the
   model one retry listing the offending figures, and otherwise falls back
   to the template. A bad number never reaches the report.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic
from pydantic import BaseModel, Field, ValidationError

from report.verify import VerificationResult, verify_text

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5-5"


class WeeklySummary(BaseModel):
    headline: str = Field(description="One sentence on the most important result of the week.")
    highlights: list[str] = Field(description="3 to 5 short factual bullet points.")
    watch_items: list[str] = Field(
        description="0 to 3 bullet points on risks or things to watch, grounded in the metrics."
    )

    def as_text(self) -> str:
        return "\n".join([self.headline, *self.highlights, *self.watch_items])


class Summarizer(Protocol):
    name: str

    def summarize(self, metrics: dict[str, Any], feedback: str | None = None) -> WeeklySummary:
        ...


class SummaryError(RuntimeError):
    pass


def money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def change(entry: dict[str, Any], is_money: bool = False) -> str:
    delta, pct = entry["delta"], entry["delta_pct"]
    if delta is None:
        return "no prior week to compare"
    if delta == 0:
        return "flat week over week"
    direction = "up" if delta > 0 else "down"
    amount = money(abs(delta)) if is_money else f"{abs(delta):g}"
    pct_text = f" ({abs(pct)}%)" if pct is not None else ""
    return f"{direction} {amount}{pct_text} week over week"


class TemplateSummarizer:
    name = "template"

    def summarize(self, metrics: dict[str, Any], feedback: str | None = None) -> WeeklySummary:
        closed, new, wow = metrics["closed"], metrics["new_deals"], metrics["week_over_week"]
        pipeline = metrics["open_pipeline"]
        win_rate = (
            f"{closed['win_rate_pct']}%" if closed["win_rate_pct"] is not None else "n/a"
        )
        highlights = [
            f"{closed['won_count']} deals won for {money(closed['won_value'])}, "
            f"{change(wow['won_value'], is_money=True)}.",
            f"{closed['lost_count']} deals lost ({money(closed['lost_value'])}); "
            f"win rate {win_rate}.",
            f"{new['count']} new deals worth {money(new['value'])}, "
            f"{change(wow['new_deals'])}.",
            f"Open pipeline: {pipeline['deal_count']} deals worth {money(pipeline['value'])}.",
        ]
        if metrics["top_owners"]:
            top = metrics["top_owners"][0]
            highlights.append(
                f"Top owner by won value: {top['owner']} with {money(top['won_value'])} "
                f"from {top['won_count']} deals."
            )
        watch = []
        if (wow["lost_count"]["delta"] or 0) > 0:
            watch.append(f"Losses {change(wow['lost_count'])}.")
        if (wow["new_deals"]["delta"] or 0) < 0:
            watch.append(f"New deal creation {change(wow['new_deals'])}.")
        if pipeline["by_stage"]:
            biggest = max(pipeline["by_stage"], key=lambda s: s["value"] or 0)
            watch.append(
                f"Largest open stage is {biggest['stage']}: {biggest['deal_count']} deals, "
                f"{money(biggest['value'])}."
            )
        return WeeklySummary(
            headline=(
                f"Week of {metrics['week_start']}: {closed['won_count']} deals won for "
                f"{money(closed['won_value'])} at a {win_rate} win rate."
            ),
            highlights=highlights,
            watch_items=watch[:3],
        )


SYSTEM_PROMPT = """\
You write the weekly sales summary read by a sales leadership team.

You are given a JSON object of metrics that were computed with SQL. It is the \
only source of truth. Rules:
- Every figure you write must appear in the JSON. Do not add, subtract, divide \
or otherwise compute new figures. Week-over-week changes are already provided \
in `week_over_week` as `delta` and `delta_pct`; use those.
- You may round a figure or format it as currency (e.g. 3500.5 -> $3,500.50 or \
$3,501), but never change its value.
- Write dates only in YYYY-MM-DD form, and only dates present in the JSON.
- If a value is null, say it is not available. Do not estimate it.
- Do not speculate about causes, people or events that the metrics do not show.
- Be concise and plain. No marketing language.

Return a headline (one sentence), 3 to 5 highlights, and 0 to 3 watch items."""


class AnthropicSummarizer:
    """Claude-backed summarizer. The client is injectable for tests."""

    def __init__(self, client: Any | None = None, model: str | None = None) -> None:
        self.client = client if client is not None else anthropic.Anthropic()
        self.model = model or os.environ.get("LLM_MODEL", DEFAULT_MODEL)
        self.name = f"llm:{self.model}"

    def summarize(self, metrics: dict[str, Any], feedback: str | None = None) -> WeeklySummary:
        content = (
            "<metrics>\n" + json.dumps(metrics, indent=2, sort_keys=True) + "\n</metrics>\n\n"
            "Write this week's summary."
        )
        if feedback:
            content += f"\n\n{feedback}"
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                # Structured output: the SDK validates the reply against the model.
                output_format=WeeklySummary,
                # Summarising a small JSON object is easy; low effort keeps it cheap and fast.
                output_config={"effort": "low"},
                # If a safety classifier declines, retry on a fallback model server-side.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (anthropic.APIError, ValidationError) as exc:
            # The SDK has already retried 429/5xx/connection errors (max_retries=2).
            # Whatever is left is recorded and handled by the template fallback.
            raise SummaryError(f"{type(exc).__name__}: {exc}") from exc
        if response.stop_reason == "refusal":
            raise SummaryError("model declined to write the summary")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise SummaryError(f"no parsed summary (stop_reason={response.stop_reason})")
        return response.parsed_output


@dataclass
class SummaryOutcome:
    summary: WeeklySummary
    source: str  # which summarizer produced the final text
    verification: VerificationResult
    attempts: int
    notes: list[str]


def generate_summary(
    summarizer: Summarizer, metrics: dict[str, Any], max_attempts: int = 2
) -> SummaryOutcome:
    notes: list[str] = []
    feedback = None
    for attempt in range(1, max_attempts + 1):
        try:
            summary = summarizer.summarize(metrics, feedback=feedback)
        except SummaryError as exc:
            notes.append(f"attempt {attempt}: {exc}")
            break
        result = verify_text(summary.as_text(), metrics)
        if result.ok:
            return SummaryOutcome(summary, summarizer.name, result, attempt, notes)
        notes.append(f"attempt {attempt}: unsupported figures {result.unsupported}")
        log.warning("summary failed verification: %s", result.unsupported)
        feedback = (
            "Your previous draft contained figures that are not in the metrics: "
            + ", ".join(result.unsupported)
            + ". Rewrite it using only figures that appear in the metrics JSON."
        )

    if isinstance(summarizer, TemplateSummarizer):
        raise SummaryError("template summary failed verification: " + "; ".join(notes))
    notes.append("fell back to template summary")
    template = TemplateSummarizer()
    summary = template.summarize(metrics)
    return SummaryOutcome(
        summary, template.name, verify_text(summary.as_text(), metrics), max_attempts, notes
    )
