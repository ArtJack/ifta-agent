"""Pure tests for Telegram bot safety helpers.

These do not call Telegram or the Anthropic API. They guard the important
automation boundaries: who may submit, where files are stored, and how old
uploads are preserved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ifta.preflight import FilePreview, PreflightReport
from ifta.telegram_bot import (
    AuthorizationError,
    BotConfig,
    DeliveryBlockedError,
    Submission,
    check_client_identity,
    clients_for_telegram_user,
    parse_user_ids,
    prepare_submission_inbox,
    resolve_authorized_client,
    run_delivery,
    safe_filename,
)


def _write_client(
    root: Path,
    client_id: str,
    *,
    aliases: list[str] | None = None,
    telegram_user_ids: list[int] | None = None,
    profile: dict[str, object] | None = None,
) -> None:
    client_dir = root / "data" / "clients" / client_id
    client_dir.mkdir(parents=True)
    payload = {
        "client_id": client_id,
        "name": client_id.replace("_", " ").upper(),
        "aliases": aliases or [],
        "base_jurisdiction": "KY",
        "portal": "ky",
        "profile": client_id,
        "source_folder": None,
        "profile_path": "profile.json" if profile is not None else None,
        "history_path": None,
        "telegram_user_ids": telegram_user_ids or [],
        "active": True,
        "notes": "test client",
    }
    (client_dir / "client.json").write_text(json.dumps(payload), encoding="utf-8")
    if profile is not None:
        (client_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")


def test_parse_user_ids_accepts_comma_and_space_separated() -> None:
    assert parse_user_ids("123, 456 789") == (123, 456, 789)


def test_parse_user_ids_rejects_non_numeric() -> None:
    with pytest.raises(ValueError, match="Invalid Telegram user id"):
        parse_user_ids("123,abc")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("fuel report.xlsx", "fuel_report.xlsx"),
        ("../../secret.csv", "secret.csv"),
        ("DM EXPRESS / Q2.pdf", "Q2.pdf"),
        ("", "upload"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


def test_clients_for_telegram_user_reads_registry(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[111])
    _write_client(tmp_path, "other", telegram_user_ids=[222])

    matches = clients_for_telegram_user(tmp_path, 111)
    assert [rec.client_id for rec in matches] == ["dm_express"]


def test_resolve_authorized_client_allows_registered_user(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", aliases=["david"], telegram_user_ids=[111])

    rec = resolve_authorized_client(
        project_root=tmp_path,
        user_id=111,
        requested_client="david",
        admin_user_ids=(),
    )
    assert rec.client_id == "dm_express"


def test_resolve_authorized_client_rejects_wrong_user(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[111])

    with pytest.raises(AuthorizationError):
        resolve_authorized_client(
            project_root=tmp_path,
            user_id=999,
            requested_client="dm_express",
            admin_user_ids=(),
        )


def test_admin_can_choose_any_client(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[])

    rec = resolve_authorized_client(
        project_root=tmp_path,
        user_id=999,
        requested_client="dm_express",
        admin_user_ids=(999,),
    )
    assert rec.client_id == "dm_express"


def test_prepare_submission_archives_existing_uploads(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[111])
    rec = resolve_authorized_client(
        project_root=tmp_path,
        user_id=111,
        requested_client="dm_express",
        admin_user_ids=(),
    )
    inbox = tmp_path / "inbox" / "dm_express" / "Q2-2026"
    inbox.mkdir(parents=True)
    (inbox / "old.csv").write_text("old", encoding="utf-8")

    submission = prepare_submission_inbox(
        project_root=tmp_path,
        rec=rec,
        quarter="Q2-2026",
        user_id=111,
    )

    assert submission.inbox == inbox
    assert (inbox / "client.json").exists()
    assert not (inbox / "old.csv").exists()
    archives = list((tmp_path / "inbox" / "dm_express" / "_archive").glob("Q2-2026_*"))
    assert len(archives) == 1
    assert (archives[0] / "old.csv").read_text(encoding="utf-8") == "old"


def test_identity_check_blocks_filename_for_other_client(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express")
    _write_client(tmp_path, "test_logistics", aliases=["test logistics"])
    submission = Submission(
        client_id="dm_express",
        client_name="DM EXPRESS INC",
        quarter="Q2-2026",
        inbox=tmp_path / "inbox" / "dm_express" / "Q2-2026",
        out_dir=tmp_path / "outputs" / "dm_express" / "Q2-2026",
    )
    report = PreflightReport(
        inbox=submission.inbox,
        files=[FilePreview("test_logistics_fuel.xlsx", ".xlsx", 100)],
    )

    identity = check_client_identity(
        project_root=tmp_path,
        submission=submission,
        report=report,
    )

    assert identity.has_errors
    assert "test_logistics" in identity.format()


def test_identity_check_blocks_disjoint_known_trucks(tmp_path: Path) -> None:
    _write_client(
        tmp_path,
        "dm_express",
        profile={"fleet_evolution": {"trucks_ever_seen": ["2013", "2015", "2017"]}},
    )
    submission = Submission(
        client_id="dm_express",
        client_name="DM EXPRESS INC",
        quarter="Q2-2026",
        inbox=tmp_path / "inbox" / "dm_express" / "Q2-2026",
        out_dir=tmp_path / "outputs" / "dm_express" / "Q2-2026",
    )
    report = PreflightReport(
        inbox=submission.inbox,
        trucks_in_miles=["T1", "T2"],
        trucks_in_fuel=["T1", "T2"],
    )

    identity = check_client_identity(
        project_root=tmp_path,
        submission=submission,
        report=report,
    )

    assert identity.has_errors
    assert "no overlap" in identity.format()


def test_identity_check_warns_when_some_trucks_are_new(tmp_path: Path) -> None:
    _write_client(
        tmp_path,
        "dm_express",
        profile={"fleet_evolution": {"trucks_ever_seen": ["2013", "2015"]}},
    )
    submission = Submission(
        client_id="dm_express",
        client_name="DM EXPRESS INC",
        quarter="Q2-2026",
        inbox=tmp_path / "inbox" / "dm_express" / "Q2-2026",
        out_dir=tmp_path / "outputs" / "dm_express" / "Q2-2026",
    )
    report = PreflightReport(
        inbox=submission.inbox,
        trucks_in_miles=["2013", "2020"],
        trucks_in_fuel=["2013", "2020"],
    )

    identity = check_client_identity(
        project_root=tmp_path,
        submission=submission,
        report=report,
    )

    assert not identity.has_errors
    assert "2020" in identity.format()


def test_run_delivery_blocks_identity_mismatch_before_writing_outputs(tmp_path: Path) -> None:
    _write_client(
        tmp_path,
        "dm_express",
        profile={"fleet_evolution": {"trucks_ever_seen": ["2013", "2015"]}},
    )
    _write_client(tmp_path, "test_logistics", aliases=["test logistics"])
    inbox = tmp_path / "inbox" / "dm_express" / "Q2-2026"
    inbox.mkdir(parents=True)
    (inbox / "client.json").write_text(
        json.dumps({"client_id": "dm_express", "name": "DM EXPRESS INC"}),
        encoding="utf-8",
    )
    (inbox / "test_logistics_miles.csv").write_text(
        "truck,state,miles\nT1,KY,100\n",
        encoding="utf-8",
    )
    (inbox / "test_logistics_fuel.csv").write_text(
        "truck,state,gallons\nT1,KY,10\n",
        encoding="utf-8",
    )
    submission = Submission(
        client_id="dm_express",
        client_name="DM EXPRESS INC",
        quarter="Q2-2026",
        inbox=inbox,
        out_dir=tmp_path / "outputs" / "dm_express" / "Q2-2026",
    )

    with pytest.raises(DeliveryBlockedError, match="CLIENT_IDENTITY_MISMATCH"):
        run_delivery(submission, BotConfig(project_root=tmp_path, token="dummy", skip_agent=True))

    assert not submission.out_dir.exists()
