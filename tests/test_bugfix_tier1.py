"""Regression tests for the two filing-corrupting bugs.

BUG-001 — duplicate upload names silently overwrote each other.
BUG-003 — an unknown/mistyped --client silently fell back to the shared quarter
          inbox and processed another carrier's data.
"""

import io
from pathlib import Path

import pytest

from ifta.client import ClientInboxError, resolve_inbox, resolve_output_dir
from ifta.web.app import _save_upload, _unique_path


class _Upload:
    """Minimal stand-in for starlette's UploadFile (_save_upload uses only these)."""

    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self.file = io.BytesIO(content)


# --- BUG-001 ---------------------------------------------------------------


def test_unique_path_suffixes_collisions(tmp_path):
    (tmp_path / "file_same.csv").write_bytes(b"x")
    assert _unique_path(tmp_path / "file_same.csv").name == "file_same_2.csv"
    (tmp_path / "file_same_2.csv").write_bytes(b"x")
    assert _unique_path(tmp_path / "file_same.csv").name == "file_same_3.csv"


def test_two_same_named_uploads_are_both_kept(tmp_path):
    first = _save_upload(_Upload("same.csv", b"first"), tmp_path, 10_000, prefix="file")
    second = _save_upload(_Upload("same.csv", b"second"), tmp_path, 10_000, prefix="file")
    assert first != second
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"  # not overwritten
    assert {first.name, second.name} == {"file_same.csv", "file_same_2.csv"}


def test_uploads_that_sanitize_to_the_same_name_dont_collide(tmp_path):
    # "a b.csv" and "a_b.csv" both sanitize to file_a_b.csv.
    one = _save_upload(_Upload("a b.csv", b"one"), tmp_path, 10_000, prefix="file")
    two = _save_upload(_Upload("a_b.csv", b"two"), tmp_path, 10_000, prefix="file")
    assert one != two
    assert one.read_bytes() == b"one"
    assert two.read_bytes() == b"two"


# --- BUG-003 ---------------------------------------------------------------


@pytest.fixture
def tenant_root(tmp_path: Path) -> Path:
    """A hermetic project root whose shared Q1-2026 inbox is owned by carrier_a.

    Replaces the old dependence on the real (untracked) inbox/Q4-2025 ownership
    marker, so the cross-tenant guard is tested against synthetic data.
    """
    import json

    shared = tmp_path / "inbox" / "Q1-2026"
    shared.mkdir(parents=True)
    (shared / "client.json").write_text(
        json.dumps({"client_id": "carrier_a", "name": "CARRIER A"}), encoding="utf-8"
    )
    return tmp_path


def test_unknown_client_is_rejected_not_silently_reassigned(tenant_root: Path) -> None:
    # The shared inbox belongs to carrier_a; a typo must NOT inherit its data.
    with pytest.raises(ClientInboxError):
        resolve_inbox(tenant_root, "Q1-2026", "does_not_exist")
    with pytest.raises(ClientInboxError):
        resolve_output_dir(tenant_root, "Q1-2026", "does_not_exist")


def test_cross_tenant_client_is_rejected(tenant_root: Path) -> None:
    # Asking for carrier_b against carrier_a's shared quarter inbox must refuse.
    with pytest.raises(ClientInboxError):
        resolve_inbox(tenant_root, "Q1-2026", "carrier_b")


def test_matching_client_still_uses_the_shared_inbox(tenant_root: Path) -> None:
    # The legitimate single-tenant path: the shared inbox IS carrier_a's.
    assert resolve_inbox(tenant_root, "Q1-2026", "carrier_a") == tenant_root / "inbox" / "Q1-2026"


def test_no_client_uses_shared_inbox_unchanged(tenant_root: Path) -> None:
    assert resolve_inbox(tenant_root, "Q1-2026") == tenant_root / "inbox" / "Q1-2026"
    assert resolve_output_dir(tenant_root, "Q1-2026") == tenant_root / "outputs" / "Q1-2026"
