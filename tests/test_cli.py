from __future__ import annotations

from pipeline.cli import main
from tests.test_metrics import load_fixture


def test_run_then_report_without_llm(tmp_path, capsys):
    db = f"sqlite:///{tmp_path / 'crm.db'}"
    # In-process mock injects 429s, 503s and bad records; the run must still succeed.
    assert main(["run", "--in-process-mock", "--db-url", db]) == 0
    assert "rejected" in capsys.readouterr().out

    out_dir = tmp_path / "reports"
    assert main(["report", "--db-url", db, "--week", "2026-11-02", "--no-llm",
                 "--out-dir", str(out_dir)]) == 0
    md = (out_dir / "weekly-2026-11-02.md").read_text()
    assert "Summary source: template. Figure check: passed" in md


def test_report_on_fixture(tmp_path):
    from sqlalchemy import create_engine

    from pipeline.load import create_schema

    db = f"sqlite:///{tmp_path / 'fixture.db'}"
    eng = create_engine(db)
    create_schema(eng)
    load_fixture(eng)
    assert main(["report", "--db-url", db, "--week", "2026-11-02", "--no-llm",
                 "--out-dir", str(tmp_path)]) == 0
    assert "3 deals won for $3,500.50" in (tmp_path / "weekly-2026-11-02.md").read_text()


def test_report_rejects_non_monday(tmp_path, capsys):
    db = f"sqlite:///{tmp_path / 'x.db'}"
    assert main(["report", "--db-url", db, "--week", "2026-11-03", "--no-llm",
                 "--out-dir", str(tmp_path)]) == 2
    assert "Monday" in capsys.readouterr().err
