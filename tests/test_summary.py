from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from pipeline.load import create_schema
from report.metrics import compute_metrics
from report.render import write_report
from report.summarize import (
    SYSTEM_PROMPT,
    AnthropicSummarizer,
    TemplateSummarizer,
    WeeklySummary,
    generate_summary,
)
from report.verify import verify_text
from tests.test_metrics import WEEK, load_fixture


@pytest.fixture(scope="module")
def metrics():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    create_schema(eng)
    load_fixture(eng)
    return compute_metrics(eng, WEEK, trend_weeks=3)


class FakeAnthropic:
    """Mimics ``client.beta.messages.parse`` and records every call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if reply == "refusal":
            return SimpleNamespace(stop_reason="refusal", parsed_output=None)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=reply)


GOOD = WeeklySummary(
    headline="Week of 2026-11-02: 3 deals won for $3,500.50, a 75.0% win rate.",
    highlights=[
        "Won value up $2,000.50 (133.4%) week over week.",
        "New deal value down 57.3% to $2,050.25.",
        "Bob led with $2,000 in won value.",
    ],
    watch_items=["Open pipeline is $5.35k across 3 deals."],
)
BAD = WeeklySummary(
    headline="Week of 2026-11-02: 9 deals won for $987,654.",
    highlights=["Win rate hit 80%, the best since 2026-01-05."],
    watch_items=[],
)


# ------------------------------------------------------------ verification
def test_good_summary_passes_figure_check(metrics):
    result = verify_text(GOOD.as_text(), metrics)
    assert result.ok, result.unsupported
    assert result.checked >= 10


def test_hallucinated_numbers_fail_figure_check(metrics):
    result = verify_text(BAD.as_text(), metrics)
    assert not result.ok
    assert set(result.unsupported) == {"9", "$987,654", "80%", "2026-01-05"}


@pytest.mark.parametrize(
    "text,ok",
    [
        ("$3,501", True),  # 3500.5 rounded to a whole dollar
        ("$3.5k", True),
        ("$3.6k", False),  # wrong rounding of 3500.5
        ("75%", True),
        ("75.5%", False),
        ("down 50%", True),  # sign-agnostic: delta_pct is -50.0
        ("Q4 and v2", True),  # digits glued to words are not figures
        ("$12,345,678", False),
    ],
)
def test_rounding_rules(metrics, text, ok):
    assert verify_text(text, metrics).ok is ok


def test_template_summary_always_verifies(metrics):
    summary = TemplateSummarizer().summarize(metrics)
    assert verify_text(summary.as_text(), metrics).ok


# --------------------------------------------------------------- mocked LLM
def test_llm_summarizer_sends_only_metrics(metrics, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "claude-haiku-5-5")
    fake = FakeAnthropic([GOOD])
    outcome = generate_summary(AnthropicSummarizer(client=fake), metrics)

    assert outcome.summary == GOOD
    assert outcome.source == "llm:claude-haiku-5-5"
    assert outcome.verification.ok and outcome.attempts == 1
    call = fake.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_format"] is WeeklySummary
    assert call["system"] == SYSTEM_PROMPT
    content = call["messages"][0]["content"]
    sent = json.loads(content.split("<metrics>")[1].split("</metrics>")[0])
    assert sent == metrics  # exactly the computed numbers, nothing else


def test_llm_gets_one_retry_with_feedback(metrics):
    fake = FakeAnthropic([BAD, GOOD])
    outcome = generate_summary(AnthropicSummarizer(client=fake, model="m"), metrics)

    assert outcome.summary == GOOD and outcome.attempts == 2
    feedback = fake.calls[1]["messages"][0]["content"]
    assert "$987,654" in feedback and "only figures" in feedback


def test_persistent_hallucination_falls_back_to_template(metrics):
    fake = FakeAnthropic([BAD, BAD])
    outcome = generate_summary(AnthropicSummarizer(client=fake, model="m"), metrics)

    assert outcome.source == "template"
    assert outcome.verification.ok
    assert "fell back to template summary" in outcome.notes


def test_refusal_falls_back_to_template(metrics):
    fake = FakeAnthropic(["refusal"])
    outcome = generate_summary(AnthropicSummarizer(client=fake, model="m"), metrics)
    assert outcome.source == "template"
    assert len(fake.calls) == 1  # a refusal is not retried with the same prompt


# ------------------------------------------------------------------ render
def test_report_files_written(metrics, tmp_path):
    outcome = generate_summary(AnthropicSummarizer(client=FakeAnthropic([GOOD]), model="m"),
                               metrics)
    paths = write_report(metrics, outcome, tmp_path, WEEK)

    md = paths["markdown"].read_text()
    assert md.startswith("# Weekly Sales Report: 2026-11-02 to 2026-11-08")
    assert GOOD.headline in md
    assert "| Deals won | 3 | 1 | +2 (+200.0%) |" in md
    assert "| 1 | Bob | 1 | $2,000.00 |" in md
    page = paths["html"].read_text()
    assert "<table>" in page and "Figure check: passed" in page
    assert json.loads(paths["metrics"].read_text()) == metrics
