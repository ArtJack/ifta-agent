"""Pytest configuration — exposes `tests/` as the test root.

The package itself is importable thanks to `pythonpath = ["src"]` in
pyproject.toml, so individual test files don't need `sys.path` hacks.

This module also provides shared helpers that keep tests hermetic:

* ``cli_project_root`` redirects the ``ifta`` CLI's module-level
  ``PROJECT_ROOT`` at a throwaway directory, so commands like ``onboard``
  never scaffold real client folders into the tracked repo.
* ``rates_or_skip`` fetches an IFTA rate matrix but skips the test when the
  quarter is neither cached locally nor reachable over the network (e.g. a
  sandboxed CI box with no outbound internet).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def cli_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the ``ifta`` CLI at an isolated, throwaway project root.

    ``onboard`` (and any other command) writes into ``ifta.cli.PROJECT_ROOT``,
    a module-level constant derived from ``__file__``. Without this redirect a
    test that invokes ``onboard`` scaffolds ``data/clients/<id>/`` and
    ``inbox/<id>/`` into the real, tracked repository — polluting the working
    tree and coupling the assertions to whatever private clients happen to
    exist on the machine running the suite. Redirecting keeps every such test
    hermetic and idempotent.
    """
    import ifta.cli as cli

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    return tmp_path


def rates_or_skip(quarter: str):
    """Return the IFTA rate matrix for ``quarter`` or skip the test.

    ``fetch_rates`` serves the rate table from ``data/rates/<qkey>.csv`` when
    it's cached and otherwise downloads it from iftach.org. Quarters without a
    committed cache file (e.g. a brand-new quarter) therefore need network
    access. On an offline box that fetch fails; rather than report a red
    failure for a purely environmental reason, skip with a clear message. When
    the matrix *is* available (cached or online) the test runs normally.
    """
    import requests

    from ifta.rates import fetch_rates

    try:
        return fetch_rates(quarter)
    except (requests.RequestException, RuntimeError) as exc:
        pytest.skip(
            f"IFTA rate matrix for {quarter} is not cached locally and could "
            f"not be fetched (offline?): {exc}"
        )
