"""Pure tests for Telegram bot safety helpers.

These do not call Telegram or the Anthropic API. They guard the important
automation boundaries: who may submit, where files are stored, and how old
uploads are preserved.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from ifta.preflight import FilePreview, PreflightReport
from ifta.telegram_bot import (
    CB_CANCEL,
    CB_CONTACT_APPROVE,
    CB_PENDING_IGNORE,
    CB_PENDING_PICK_CLIENT,
    CB_PENDING_PICK_USER,
    CB_PHONE_PREAUTH,
    CB_REVOKE_CONFIRM,
    CB_REVOKE_PICK,
    AuthorizationError,
    BotConfig,
    DeliveryBlockedError,
    Submission,
    _admin_keyboard,
    _all_approved_user_ids,
    _approved_keyboard,
    _build_pending_user_list_markup,
    _build_revoke_user_list_markup,
    _keyboard_for_user,
    _normalize_username,
    _unapproved_keyboard,
    add_pending_user,
    approve_telegram_user,
    authorize_submission_access,
    check_client_identity,
    clients_for_telegram_user,
    current_filing_quarter,
    format_user_label,
    get_known_user,
    get_phone_preauth,
    load_pending_users,
    load_phone_preauth,
    load_preauth,
    load_telegram_access,
    normalize_phone,
    parse_user_ids,
    prepare_submission_inbox,
    remove_pending_user,
    remove_phone_preauth,
    remove_preauth,
    resolve_authorized_client,
    revoke_telegram_user,
    run_delivery,
    safe_filename,
    set_phone_preauth,
    set_preauth,
    upsert_known_user,
    write_telegram_access,
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


def test_approve_telegram_user_writes_local_allowlist(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", aliases=["david"], telegram_user_ids=[])

    rec = approve_telegram_user(
        project_root=tmp_path,
        user_id=111,
        requested_client="david",
    )

    assert rec.client_id == "dm_express"
    assert load_telegram_access(tmp_path) == {"dm_express": {111}}
    assert [match.client_id for match in clients_for_telegram_user(tmp_path, 111)] == [
        "dm_express"
    ]


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

    with pytest.raises(AuthorizationError, match="not allowed"):
        resolve_authorized_client(
            project_root=tmp_path,
            user_id=999,
            requested_client="dm_express",
            admin_user_ids=(),
        )


def test_resolve_authorized_client_hides_registry_from_unapproved_user(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[111])
    _write_client(tmp_path, "menshikov_llc", telegram_user_ids=[222])

    with pytest.raises(AuthorizationError) as exc:
        resolve_authorized_client(
            project_root=tmp_path,
            user_id=999,
            requested_client="not_a_real_client",
            admin_user_ids=(),
        )

    message = str(exc.value)
    assert message == "Unknown or unauthorized client."
    assert "dm_express" not in message
    assert "menshikov_llc" not in message


def test_admin_can_choose_any_client(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[])

    rec = resolve_authorized_client(
        project_root=tmp_path,
        user_id=999,
        requested_client="dm_express",
        admin_user_ids=(999,),
    )
    assert rec.client_id == "dm_express"


def test_authorize_submission_access_rechecks_existing_session(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[])
    approve_telegram_user(project_root=tmp_path, user_id=111, requested_client="dm_express")
    submission = Submission(
        client_id="dm_express",
        client_name="DM EXPRESS INC",
        quarter="Q2-2026",
        inbox=tmp_path / "inbox" / "dm_express" / "Q2-2026",
        out_dir=tmp_path / "outputs" / "dm_express" / "Q2-2026",
    )

    rec = authorize_submission_access(
        project_root=tmp_path,
        user_id=111,
        submission=submission,
        admin_user_ids=(),
    )
    assert rec.client_id == "dm_express"

    with pytest.raises(AuthorizationError, match="no longer authorized"):
        authorize_submission_access(
            project_root=tmp_path,
            user_id=999,
            submission=submission,
            admin_user_ids=(),
        )


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


# ─── Reply-keyboard helpers ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "today,expected",
    [
        # February: prior quarter is Q4 of the previous year.
        (datetime(2026, 2, 14), "Q4-2025"),
        # April: just finished Q1.
        (datetime(2026, 4, 1), "Q1-2026"),
        # May: still filing Q1.
        (datetime(2026, 5, 18), "Q1-2026"),
        # July: just finished Q2.
        (datetime(2026, 7, 5), "Q2-2026"),
        # October: just finished Q3.
        (datetime(2026, 10, 2), "Q3-2026"),
        # End of December: still Q3 (Q4 hasn't closed yet).
        (datetime(2026, 12, 31), "Q3-2026"),
        # January: prior quarter is Q4 of last year.
        (datetime(2027, 1, 15), "Q4-2026"),
    ],
)
def test_current_filing_quarter(today: datetime, expected: str) -> None:
    assert current_filing_quarter(today) == expected


def test_unapproved_keyboard_has_only_id_button() -> None:
    kb = _unapproved_keyboard()
    buttons = [btn.text for row in kb.keyboard for btn in row]
    assert buttons == ["/id"]


def test_approved_keyboard_has_workflow_buttons() -> None:
    kb = _approved_keyboard("Q1-2026")
    buttons = [btn.text for row in kb.keyboard for btn in row]
    assert "/new Q1-2026" in buttons
    assert "/status" in buttons
    assert "/process" in buttons
    assert "/cancel" in buttons
    assert "/help" in buttons


def test_approved_keyboard_defaults_to_current_filing_quarter() -> None:
    kb = _approved_keyboard()
    buttons = [btn.text for row in kb.keyboard for btn in row]
    # Default uses live datetime — assert the /new button starts with /new Q
    new_buttons = [b for b in buttons if b.startswith("/new ")]
    assert len(new_buttons) == 1
    assert new_buttons[0].split()[1].startswith("Q")


def test_keyboard_for_user_unapproved(tmp_path: Path) -> None:
    kb = _keyboard_for_user(project_root=tmp_path, user_id=999, admin_user_ids=())
    buttons = [btn.text for row in kb.keyboard for btn in row]
    assert buttons == ["/id"]


def test_keyboard_for_user_admin(tmp_path: Path) -> None:
    """Admin sees no reply keyboard — workflow is event-driven via inline DMs."""
    from telegram import ReplyKeyboardRemove

    kb = _keyboard_for_user(
        project_root=tmp_path, user_id=42, admin_user_ids=(42,)
    )
    assert isinstance(kb, ReplyKeyboardRemove)


def test_keyboard_for_user_no_user_id(tmp_path: Path) -> None:
    kb = _keyboard_for_user(project_root=tmp_path, user_id=None, admin_user_ids=())
    buttons = [btn.text for row in kb.keyboard for btn in row]
    assert buttons == ["/id"]


def test_keyboard_for_user_approved_customer(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[5550])
    kb = _keyboard_for_user(
        project_root=tmp_path, user_id=5550, admin_user_ids=()
    )
    buttons = [btn.text for row in kb.keyboard for btn in row]
    assert "/process" in buttons


# ─── Pending users + preauth ───────────────────────────────────────────────


def test_add_pending_user_and_load(tmp_path: Path) -> None:
    add_pending_user(tmp_path, user_id=111, username="alice", name="Alice")
    add_pending_user(tmp_path, user_id=222, username=None, name="Bob")
    items = sorted(load_pending_users(tmp_path), key=lambda p: p.user_id)
    assert [(p.user_id, p.username, p.name) for p in items] == [
        (111, "alice", "Alice"),
        (222, None, "Bob"),
    ]


def test_add_pending_user_replaces_existing(tmp_path: Path) -> None:
    add_pending_user(tmp_path, user_id=111, username="alice", name="Alice")
    # Same user, new name (e.g. they updated their profile)
    add_pending_user(tmp_path, user_id=111, username="alice_new", name="Alice S")
    items = load_pending_users(tmp_path)
    assert len(items) == 1
    assert items[0].username == "alice_new"
    assert items[0].name == "Alice S"


def test_remove_pending_user(tmp_path: Path) -> None:
    add_pending_user(tmp_path, user_id=111, username="alice", name="Alice")
    add_pending_user(tmp_path, user_id=222, username="bob", name="Bob")
    remove_pending_user(tmp_path, 111)
    items = load_pending_users(tmp_path)
    assert [p.user_id for p in items] == [222]


def test_pending_section_preserved_on_clients_write(tmp_path: Path) -> None:
    """write_telegram_access must not clobber pending/preauth."""
    add_pending_user(tmp_path, user_id=111, username="alice", name="Alice")
    set_preauth(tmp_path, "@bob", "dm_express")
    # Now a regular client-approval write — pending/preauth must survive.
    write_telegram_access(tmp_path, {"dm_express": {999}})
    assert load_pending_users(tmp_path) and load_pending_users(tmp_path)[0].user_id == 111
    assert load_preauth(tmp_path) == {"bob": "dm_express"}
    assert load_telegram_access(tmp_path) == {"dm_express": {999}}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@Alice", "alice"),
        ("Alice", "alice"),
        ("  @BOB_42  ", "bob_42"),
        ("@", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_username(raw: str | None, expected: str | None) -> None:
    assert _normalize_username(raw) == expected


def test_set_and_load_preauth(tmp_path: Path) -> None:
    set_preauth(tmp_path, "@Alice", "dm_express")
    set_preauth(tmp_path, "bob", "test_logistics")
    assert load_preauth(tmp_path) == {
        "alice": "dm_express",
        "bob": "test_logistics",
    }


def test_set_preauth_rejects_empty_username(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Username is required"):
        set_preauth(tmp_path, "@", "dm_express")


def test_remove_preauth(tmp_path: Path) -> None:
    set_preauth(tmp_path, "@alice", "dm_express")
    assert remove_preauth(tmp_path, "@ALICE") is True
    assert load_preauth(tmp_path) == {}
    # Removing again is a no-op (returns False).
    assert remove_preauth(tmp_path, "@alice") is False


# ─── scaffold_client ───────────────────────────────────────────────────────


def test_scaffold_client_creates_files(tmp_path: Path) -> None:
    from ifta.client import scaffold_client

    result = scaffold_client(
        tmp_path, "ABC_Trucking", name="ABC TRUCKING LLC", base_state="TX"
    )
    assert result.client_id == "abc_trucking"
    assert (tmp_path / "data" / "clients" / "abc_trucking" / "client.json").exists()
    assert (tmp_path / "data" / "clients" / "abc_trucking" / "profile.json").exists()
    assert result.inbox_dir is not None and result.inbox_dir.exists()

    meta = json.loads(
        (tmp_path / "data" / "clients" / "abc_trucking" / "client.json").read_text()
    )
    assert meta["name"] == "ABC TRUCKING LLC"
    assert meta["base_jurisdiction"] == "TX"


def test_scaffold_client_refuses_duplicate(tmp_path: Path) -> None:
    from ifta.client import ScaffoldError, scaffold_client

    _write_client(tmp_path, "dm_express")
    with pytest.raises(ScaffoldError, match="already resolves to registered client"):
        scaffold_client(tmp_path, "dm_express")


def test_scaffold_client_rejects_empty_id(tmp_path: Path) -> None:
    from ifta.client import ScaffoldError, scaffold_client

    with pytest.raises(ScaffoldError, match="alphanumeric"):
        scaffold_client(tmp_path, "---")


# ─── /unapprove + admin keyboard ───────────────────────────────────────────


def test_revoke_telegram_user_removes_from_all_clients(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express")
    _write_client(tmp_path, "test_logistics")
    approve_telegram_user(
        project_root=tmp_path, user_id=42, requested_client="dm_express"
    )
    approve_telegram_user(
        project_root=tmp_path, user_id=42, requested_client="test_logistics"
    )
    approve_telegram_user(
        project_root=tmp_path, user_id=99, requested_client="dm_express"
    )

    removed = revoke_telegram_user(tmp_path, 42)
    assert sorted(removed) == ["dm_express", "test_logistics"]

    access = load_telegram_access(tmp_path)
    assert 42 not in access.get("dm_express", set())
    assert 42 not in access.get("test_logistics", set())
    # Other users untouched.
    assert 99 in access.get("dm_express", set())


def test_revoke_telegram_user_returns_empty_when_unknown(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express")
    assert revoke_telegram_user(tmp_path, 12345) == []


def test_admin_keyboard_is_remove_marker(tmp_path: Path) -> None:
    """Masterpiece admin UX: no reply keyboard. Bot is event-driven via inline
    DMs (proactive ping on new pending users; paperclip → Contact to add)."""
    from telegram import ReplyKeyboardRemove

    _write_client(tmp_path, "dm_express")
    kb = _admin_keyboard(tmp_path, quarter="Q1-2026")
    assert isinstance(kb, ReplyKeyboardRemove)


def test_keyboard_router_picks_admin_for_admin_id(tmp_path: Path) -> None:
    """Admin routes to ReplyKeyboardRemove — no keyboard at all."""
    from telegram import ReplyKeyboardRemove

    _write_client(tmp_path, "dm_express")
    kb = _keyboard_for_user(
        project_root=tmp_path, user_id=392147409, admin_user_ids=(392147409,)
    )
    assert isinstance(kb, ReplyKeyboardRemove)


def test_keyboard_router_customer_does_not_get_admin_buttons(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express", telegram_user_ids=[5550])
    kb = _keyboard_for_user(
        project_root=tmp_path, user_id=5550, admin_user_ids=()
    )
    buttons = [btn.text for row in kb.keyboard for btn in row]
    assert "/pending" not in buttons
    assert "/preauth" not in buttons
    assert "/onboard" not in buttons
    assert "/unapprove" not in buttons


# ─── known_users + inline-wizard plumbing ──────────────────────────────────


def test_upsert_known_user_round_trip(tmp_path: Path) -> None:
    upsert_known_user(tmp_path, user_id=111, username="alice", name="Alice")
    known = get_known_user(tmp_path, 111)
    assert known is not None
    assert known.user_id == 111
    assert known.username == "alice"
    assert known.name == "Alice"
    assert known.last_seen  # ISO timestamp written


def test_upsert_known_user_refreshes_label(tmp_path: Path) -> None:
    upsert_known_user(tmp_path, user_id=111, username="alice", name="Alice")
    upsert_known_user(tmp_path, user_id=111, username="alice_new", name="Alice S")
    known = get_known_user(tmp_path, 111)
    assert known is not None
    assert known.username == "alice_new"
    assert known.name == "Alice S"


def test_get_known_user_returns_none_when_missing(tmp_path: Path) -> None:
    assert get_known_user(tmp_path, 999) is None


def test_format_user_label_variants(tmp_path: Path) -> None:
    assert format_user_label(None, 42) == "id=42"
    upsert_known_user(tmp_path, user_id=42, username="alice", name="Alice Smith")
    assert format_user_label(get_known_user(tmp_path, 42), 42) == "@alice (Alice Smith)"
    upsert_known_user(tmp_path, user_id=43, username="bob", name=None)
    assert format_user_label(get_known_user(tmp_path, 43), 43) == "@bob"
    upsert_known_user(tmp_path, user_id=44, username=None, name="Carol")
    assert format_user_label(get_known_user(tmp_path, 44), 44) == "Carol (id=44)"
    upsert_known_user(tmp_path, user_id=45, username=None, name=None)
    assert format_user_label(get_known_user(tmp_path, 45), 45) == "id=45"


def test_all_approved_user_ids_buckets_by_client(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express")
    _write_client(tmp_path, "test_logistics")
    approve_telegram_user(project_root=tmp_path, user_id=10, requested_client="dm_express")
    approve_telegram_user(project_root=tmp_path, user_id=10, requested_client="test_logistics")
    approve_telegram_user(project_root=tmp_path, user_id=20, requested_client="dm_express")
    grouped = _all_approved_user_ids(tmp_path)
    assert sorted(grouped[10]) == ["dm_express", "test_logistics"]
    assert grouped[20] == ["dm_express"]


def test_pending_inline_keyboard_per_user(tmp_path: Path) -> None:
    add_pending_user(tmp_path, user_id=10, username="alice", name="Alice")
    add_pending_user(tmp_path, user_id=20, username=None, name="Bob")
    items = sorted(load_pending_users(tmp_path), key=lambda p: p.user_id)
    markup = _build_pending_user_list_markup(items)
    # First two rows are user buttons; final row is refresh+close.
    callback_data = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert f"{CB_PENDING_PICK_USER}:10" in callback_data
    assert f"{CB_PENDING_PICK_USER}:20" in callback_data
    assert CB_CANCEL in callback_data


def test_revoke_inline_keyboard_labels_use_known_user(tmp_path: Path) -> None:
    _write_client(tmp_path, "dm_express")
    upsert_known_user(tmp_path, user_id=10, username="alice", name="Alice")
    approve_telegram_user(project_root=tmp_path, user_id=10, requested_client="dm_express")
    by_user = _all_approved_user_ids(tmp_path)
    markup = _build_revoke_user_list_markup(tmp_path, by_user=by_user)
    user_buttons = [
        btn
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data and btn.callback_data.startswith(f"{CB_REVOKE_PICK}:")
    ]
    assert len(user_buttons) == 1
    assert "@alice" in user_buttons[0].text


def test_callback_data_within_telegram_64_byte_limit(tmp_path: Path) -> None:
    """Telegram caps callback_data at 64 bytes — make sure ours stays under."""
    _write_client(tmp_path, "a_very_long_client_id_for_test_only")
    add_pending_user(tmp_path, user_id=9999999999, username="x", name="x")
    items = load_pending_users(tmp_path)
    markup = _build_pending_user_list_markup(items)
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data is not None:
                assert len(btn.callback_data.encode("utf-8")) <= 64

    # Same check for the client picker (longest single message in the flow).
    from ifta.telegram_bot import _build_client_picker_markup

    picker = _build_client_picker_markup(
        tmp_path, callback_prefix=CB_PENDING_PICK_CLIENT, user_id=9999999999
    )
    for row in picker.inline_keyboard:
        for btn in row:
            if btn.callback_data is not None:
                assert len(btn.callback_data.encode("utf-8")) <= 64


def test_revoke_confirm_callback_format(tmp_path: Path) -> None:
    """Confirmation prefix splits cleanly into action:user_id."""
    payload = f"{CB_REVOKE_CONFIRM}:392147409"
    prefix, arg = payload.split(":", 1)
    assert prefix == CB_REVOKE_CONFIRM
    assert int(arg) == 392147409


# ─── Phone preauth + contact share ────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+1 916 936 5441", "+19169365441"),
        ("+1 (916) 936-5441", "+19169365441"),
        ("19169365441", "+19169365441"),
        ("+19169365441", "+19169365441"),
        ("", None),
        (None, None),
        ("not a phone", None),
        ("   ", None),
    ],
)
def test_normalize_phone(raw: str | None, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


def test_set_and_load_phone_preauth(tmp_path: Path) -> None:
    set_phone_preauth(tmp_path, "+1 916 936 5441", "dm_express", name="Nastya")
    set_phone_preauth(tmp_path, "+19998887777", "test_logistics")
    items = load_phone_preauth(tmp_path)
    assert set(items) == {"+19169365441", "+19998887777"}
    assert items["+19169365441"]["client_id"] == "dm_express"
    assert items["+19169365441"]["name"] == "Nastya"


def test_phone_preauth_matches_any_format(tmp_path: Path) -> None:
    set_phone_preauth(tmp_path, "+19169365441", "dm_express")
    # Same number written different ways — all must include the country code.
    for variant in ["+1 916 936 5441", "1-916-936-5441", "+1 (916) 936-5441", "+19169365441"]:
        entry = get_phone_preauth(tmp_path, variant)
        assert entry is not None, f"failed for variant {variant!r}"
        assert entry["client_id"] == "dm_express"


def test_phone_preauth_does_not_match_missing_country_code(tmp_path: Path) -> None:
    """Bot can't guess country code — 10-digit local format must not match E.164."""
    set_phone_preauth(tmp_path, "+19169365441", "dm_express")
    assert get_phone_preauth(tmp_path, "(916) 936-5441") is None
    assert get_phone_preauth(tmp_path, "9169365441") is None


def test_set_phone_preauth_rejects_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Phone number is required"):
        set_phone_preauth(tmp_path, "", "dm_express")


def test_remove_phone_preauth(tmp_path: Path) -> None:
    set_phone_preauth(tmp_path, "+19169365441", "dm_express")
    assert remove_phone_preauth(tmp_path, "+1 916 936 5441") is True
    assert load_phone_preauth(tmp_path) == {}
    assert remove_phone_preauth(tmp_path, "+19169365441") is False


def test_phone_preauth_preserved_alongside_other_sections(tmp_path: Path) -> None:
    """Writes to other sections (clients, preauth) must not clobber phone preauth."""
    set_phone_preauth(tmp_path, "+19169365441", "dm_express", name="Nastya")
    set_preauth(tmp_path, "@alice", "dm_express")
    add_pending_user(tmp_path, user_id=111, username="bob", name="Bob")
    write_telegram_access(tmp_path, {"dm_express": {999}})
    # Everything should still be there.
    assert load_phone_preauth(tmp_path).get("+19169365441") is not None
    assert load_preauth(tmp_path) == {"alice": "dm_express"}
    assert any(p.user_id == 111 for p in load_pending_users(tmp_path))
    assert load_telegram_access(tmp_path) == {"dm_express": {999}}


def test_unapproved_keyboard_offers_share_contact_when_phone_preauth_exists(tmp_path: Path) -> None:
    set_phone_preauth(tmp_path, "+19169365441", "dm_express")
    kb = _unapproved_keyboard(tmp_path)
    buttons = [btn for row in kb.keyboard for btn in row]
    share = [b for b in buttons if b.request_contact]
    assert len(share) == 1
    assert "Share my contact" in share[0].text


def test_unapproved_keyboard_omits_share_contact_when_no_phone_preauth(tmp_path: Path) -> None:
    kb = _unapproved_keyboard(tmp_path)
    buttons = [btn for row in kb.keyboard for btn in row]
    assert all(not b.request_contact for b in buttons)
    assert [b.text for b in buttons] == ["/id"]


def test_pending_ignore_callback_format() -> None:
    """Ignore callback splits cleanly into action:user_id."""
    payload = f"{CB_PENDING_IGNORE}:5409245594"
    prefix, arg = payload.split(":", 1)
    assert prefix == CB_PENDING_IGNORE
    assert int(arg) == 5409245594


def test_proactive_new_pending_notification_buttons() -> None:
    """Smoke-test the inline keyboard structure built when DMing admins about
    a brand-new pending user (the masterpiece event-driven flow)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✓ Approve",
                    callback_data=f"{CB_PENDING_PICK_USER}:5409245594",
                ),
                InlineKeyboardButton(
                    "✕ Ignore",
                    callback_data=f"{CB_PENDING_IGNORE}:5409245594",
                ),
            ]
        ]
    )
    callback_data = [
        btn.callback_data for row in markup.inline_keyboard for btn in row
    ]
    assert f"{CB_PENDING_PICK_USER}:5409245594" in callback_data
    assert f"{CB_PENDING_IGNORE}:5409245594" in callback_data
    # Stays well under Telegram's 64-byte callback_data cap.
    for cb in callback_data:
        assert cb is not None and len(cb.encode("utf-8")) <= 64


def test_share_bot_link_markup_when_bot_username_known() -> None:
    from ifta.telegram_bot import _share_bot_link_markup

    markup = _share_bot_link_markup("eugene_ifta_bot", "Nastya")
    assert markup is not None
    buttons = [btn for row in markup.inline_keyboard for btn in row]
    assert len(buttons) == 1
    btn = buttons[0]
    assert btn.text.startswith("📤")
    assert btn.url is not None
    assert btn.url.startswith("https://t.me/share/url?")
    # Bot link is URL-encoded inside the share URL parameters.
    assert "https%3A%2F%2Ft.me%2Feugene_ifta_bot" in btn.url
    # Customer name is in the pre-filled text.
    assert "Nastya" in btn.url
    # Spaces must be %20, NOT '+' — Telegram's share dialog decodes %20 but
    # leaves '+' as literal '+' in the message body.
    assert "+" not in btn.url
    assert "%20" in btn.url


def test_share_bot_link_markup_returns_none_without_username() -> None:
    from ifta.telegram_bot import _share_bot_link_markup

    assert _share_bot_link_markup(None, "Nastya") is None
    assert _share_bot_link_markup("", "Nastya") is None


def test_share_bot_link_markup_generic_text_when_no_customer_name() -> None:
    from ifta.telegram_bot import _share_bot_link_markup

    markup = _share_bot_link_markup("eugene_ifta_bot", None)
    assert markup is not None
    btn = markup.inline_keyboard[0][0]
    assert btn.url is not None
    # "Hi, " not "Hi None"
    assert "Hi%2C" in btn.url


def test_contact_callback_data_within_telegram_limit(tmp_path: Path) -> None:
    """Phone callback uses phone-without-plus + client_id — keep under 64 bytes."""
    _write_client(tmp_path, "abc_trucking_long_name_for_test")
    from ifta.telegram_bot import _build_client_picker_for_contact

    markup = _build_client_picker_for_contact(
        tmp_path,
        callback_prefix=CB_PHONE_PREAUTH,
        arg="19169365441",
    )
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data is not None:
                assert len(btn.callback_data.encode("utf-8")) <= 64

    markup2 = _build_client_picker_for_contact(
        tmp_path,
        callback_prefix=CB_CONTACT_APPROVE,
        arg="392147409",
    )
    for row in markup2.inline_keyboard:
        for btn in row:
            if btn.callback_data is not None:
                assert len(btn.callback_data.encode("utf-8")) <= 64
