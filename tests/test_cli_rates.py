"""BUG-006: `ifta rates` must reject a bad quarter with a clean Click error,
not a raw ValueError traceback (consistent with `ifta run`)."""

from click.testing import CliRunner

from ifta.cli import main


def test_rates_bad_quarter_is_clean_click_error():
    result = CliRunner().invoke(main, ["rates", "--quarter", "2026-Q9"])
    assert result.exit_code != 0
    # The fix routes through _parse_quarter -> ClickException (SystemExit),
    # so the raw ValueError must no longer escape to the user.
    assert not isinstance(result.exception, ValueError)
