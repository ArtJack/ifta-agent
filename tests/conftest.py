"""Pytest configuration — exposes `tests/` as the test root.

The package itself is importable thanks to `pythonpath = ["src"]` in
pyproject.toml, so individual test files don't need `sys.path` hacks.
"""
