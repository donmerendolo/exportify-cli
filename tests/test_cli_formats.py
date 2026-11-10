import pytest
from click.testing import CliRunner

from exportify_cli import cli


def test_resolve_formats_cli_value_normalized_to_lowercase():
    assert cli.resolve_formats(("CSV",), {}) == ["csv"]


def test_resolve_formats_cli_invalid_value_errors(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.resolve_formats(("xml",), {})
    assert exc_info.value.code == 1
    assert "invalid format" in capsys.readouterr().err


def test_format_option_accepts_uppercase_value(tmp_path):
    runner = CliRunner()
    missing = tmp_path / "nope.toml"
    result = runner.invoke(cli.main, ["-f", "CSV", "-c", str(missing)])
    assert result.exit_code == 1
    assert "does not exist" in result.output
