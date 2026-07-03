"""Regression tests for the two filing-corrupting bugs.

BUG-001 — duplicate upload names silently overwrote each other.
BUG-003 — an unknown/mistyped --client silently fell back to the shared quarter
          inbox and processed another carrier's data.
"""

import io
import json
from pathlib import Path

import pytest

from ifta.client import ClientInboxError, resolve_inbox, resolve_output_dir
from ifta.web.app import _save_upload, _unique_path

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def owned_quarter(tmp_path: Path) -> Path:
    """A project root whose shared Q4-2025 inbox declares an owner.

    BUG-003 is about the shared ``inbox/<quarter>/`` folder: when its
    ``client.json`` names a carrier, requests for a *different* client must be
    refused rather than silently handed that carrier's data. Building the
    fixture in a temp dir keeps the test hermetic — it exercises the real
    ``resolve_inbox`` ownership logic without depending on the author's
    private ``inbox/Q4-2025/`` (which is gitignored PII and absent on clean
    checkouts).
    """
    inbox = tmp_path / "inbox" / "Q4-2025"
    inbox.mkdir(parents=True)
    (inbox / "client.json").write_text(
        json.dumps({"client_id": "menshikov_llc", "name": "MENSHIKOV LLC"}),
        encoding="utf-8",
    )
    return tmp_path


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


def test_unknown_client_is_rejected_not_silently_reassigned(owned_quarter: Path):
    # inbox/Q4-2025 belongs to menshikov_llc; a typo must NOT inherit its data.
    with pytest.raises(ClientInboxError):
        resolve_inbox(owned_quarter, "Q4-2025", "does_not_exist")
    with pytest.raises(ClientInboxError):
        resolve_output_dir(owned_quarter, "Q4-2025", "does_not_exist")


def test_cross_tenant_client_is_rejected(owned_quarter: Path):
    # Asking for dm_express against menshikov's shared quarter inbox must refuse.
    with pytest.raises(ClientInboxError):
        resolve_inbox(owned_quarter, "Q4-2025", "dm_express")


def test_matching_client_still_uses_the_shared_inbox(owned_quarter: Path):
    # The legitimate single-tenant path: the shared Q4-2025 inbox IS menshikov's.
    assert (
        resolve_inbox(owned_quarter, "Q4-2025", "menshikov_llc")
        == owned_quarter / "inbox" / "Q4-2025"
    )


def test_no_client_uses_shared_inbox_unchanged(owned_quarter: Path):
    assert resolve_inbox(owned_quarter, "Q4-2025") == owned_quarter / "inbox" / "Q4-2025"
    assert (
        resolve_output_dir(owned_quarter, "Q4-2025")
        == owned_quarter / "outputs" / "Q4-2025"
    )
