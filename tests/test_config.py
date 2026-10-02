"""Where the settings come from: CLI beats the environment beats the ini file."""

from __future__ import annotations

from pathlib import Path

from helpers import run_demo, summarize

SINGLE_TEST = ("-k", "test_orders_count")


def test_the_cli_wins_over_the_environment(pytester, demo, demo_env, monkeypatch):
    monkeypatch.setenv("CAPQUERY_MIN_TESTS", "0")
    result = run_demo(pytester, *SINGLE_TEST, "--capquery-min-tests=100")
    assert result.ret == 0
    assert summarize(result).disabled is not None


def test_the_environment_wins_over_the_ini_file(pytester, demo, demo_env, monkeypatch):
    ini = Path(demo) / "pytest.ini"
    ini.write_text(ini.read_text(encoding="utf-8") + "capquery_min_tests = 0\n", encoding="utf-8")

    # the ini file alone enables capquery for a single test
    monkeypatch.delenv("CAPQUERY_MIN_TESTS", raising=False)
    enabled = run_demo(pytester, *SINGLE_TEST)
    assert enabled.ret == 0
    summary = summarize(enabled)
    assert summary.disabled is None, str(summary)
    assert summary.created == 1

    # the environment overrules it
    monkeypatch.setenv("CAPQUERY_MIN_TESTS", "100")
    disabled = run_demo(pytester, *SINGLE_TEST)
    assert disabled.ret == 0
    assert summarize(disabled).disabled is not None


def test_a_broken_setting_is_reported(pytester, demo, demo_env, monkeypatch):
    monkeypatch.setenv("CAPQUERY_MIN_TESTS", "many")
    result = run_demo(pytester, *SINGLE_TEST)
    assert result.ret != 0
    assert "capquery_min_tests" in result.stderr.str()
