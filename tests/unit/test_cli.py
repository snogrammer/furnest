"""Smoke tests for the ``furnest`` CLI surface."""

import pytest
from typer.testing import CliRunner

import furnest
from furnest.cli import app

runner = CliRunner()


def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("web", "worker", "migrate"):
        assert command in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output


def test_version_flag_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"furnest {furnest.__version__}"


@pytest.mark.parametrize(
    ("command", "issue"),
    [("web", 6), ("worker", 8), ("migrate", 4)],
)
def test_stub_commands_fail_loudly(command: str, issue: int) -> None:
    result = runner.invoke(app, [command])

    assert result.exit_code == 1
    assert f"not implemented yet (see #{issue})" in result.output
