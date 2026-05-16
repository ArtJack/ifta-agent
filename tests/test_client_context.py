"""Client metadata and path-resolution guards."""

from pathlib import Path

from ifta.client import load_client_context, resolve_inbox

ROOT = Path(__file__).resolve().parents[1]


def test_q2_fake_data_identifies_test_logistics_from_metadata() -> None:
    inbox = resolve_inbox(ROOT, "Q2-2026")
    context = load_client_context(ROOT, "Q2-2026", inbox=inbox)

    assert inbox == ROOT / "inbox" / "Q2-2026"
    assert context.client_id == "test_logistics"
    assert context.client_name == "TEST LOGISTICS LLC"
    assert context.profile == "none"
    assert "David" in context.notes
