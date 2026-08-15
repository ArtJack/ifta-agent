"""Tests for R2 replication and the Postgres-aware snapshot path.

Fully offline: the S3 client is a hand-rolled fake and pg_dump is stubbed, so
nothing here touches a network or a database. The R2 environment variables are
cleared per test — a developer with real credentials exported must not get
different results than CI.
"""

from __future__ import annotations

import subprocess
import tarfile

import pytest

from ifta import backup as backup_mod
from ifta.backup import DEFAULT_KEEP, BackupResult, resolve_keep, run_backup
from ifta.backup_remote import (
    RemoteConfig,
    download_snapshot,
    list_remote,
    prune_remote,
    upload_snapshot,
)

R2_ENV = {
    "IFTA_BACKUP_R2_BUCKET": "ifta-backups",
    "IFTA_BACKUP_R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
    "IFTA_BACKUP_R2_ACCESS_KEY_ID": "key",
    "IFTA_BACKUP_R2_SECRET_ACCESS_KEY": "secret",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient R2 / database / retention config leaking in from the shell."""
    for name in (*R2_ENV, "IFTA_BACKUP_R2_PREFIX", "IFTA_BACKUP_KEEP", "IFTA_WEB_DB_URL"):
        monkeypatch.delenv(name, raising=False)


class FakeS3:
    """Minimal in-memory stand-in for the boto3 S3 client surface we use."""

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, bytes] = {}
        self.page_size = page_size
        self.deleted: list[str] = []

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        with open(filename, "rb") as fh:
            self.objects[key] = fh.read()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        with open(filename, "wb") as fh:
            fh.write(self.objects[key])

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        token = kwargs.get("ContinuationToken")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        start = keys.index(token) if token else 0
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        out = {"Contents": [{"Key": k} for k in page], "IsTruncated": truncated}
        if truncated:
            out["NextContinuationToken"] = keys[start + self.page_size]
        return out

    def delete_objects(self, **kwargs):
        for obj in kwargs["Delete"]["Objects"]:
            self.objects.pop(obj["Key"], None)
            self.deleted.append(obj["Key"])
        return {}


def _config(prefix: str = "snapshots") -> RemoteConfig:
    return RemoteConfig(
        bucket="ifta-backups",
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        access_key_id="key",
        secret_access_key="secret",
        prefix=prefix,
    )


def _make_data(root):
    data = root / "data"
    (data / "clients").mkdir(parents=True)
    (data / "clients" / "x.json").write_text('{"id": "x"}')
    return data


# --- configuration ---------------------------------------------------------


def test_unconfigured_remote_is_none() -> None:
    assert RemoteConfig.from_env({}) is None


def test_fully_configured_remote_parses() -> None:
    config = RemoteConfig.from_env({**R2_ENV, "IFTA_BACKUP_R2_PREFIX": "/nightly/"})
    assert config is not None
    assert config.bucket == "ifta-backups"
    assert config.prefix == "nightly"  # slashes trimmed
    assert config.key_for("ifta-data-x.tar.gz") == "nightly/ifta-data-x.tar.gz"


def test_partial_config_raises_rather_than_silently_skipping() -> None:
    """A half-set remote means backups aren't leaving the box — fail loudly."""
    partial = dict(R2_ENV)
    del partial["IFTA_BACKUP_R2_SECRET_ACCESS_KEY"]
    with pytest.raises(RuntimeError, match="partially configured"):
        RemoteConfig.from_env(partial)


def test_blank_values_count_as_unset() -> None:
    """Compose passes `KEY=` for unset optionals; that must read as 'no remote'."""
    assert RemoteConfig.from_env(dict.fromkeys(R2_ENV, "")) is None


# --- upload / list / prune / download --------------------------------------


def test_upload_then_list_round_trips(tmp_path) -> None:
    snap = tmp_path / "ifta-data-20260810T000000Z.tar.gz"
    snap.write_bytes(b"payload")
    client = FakeS3()
    config = _config()

    key = upload_snapshot(snap, config, client=client)

    assert key == "snapshots/ifta-data-20260810T000000Z.tar.gz"
    assert client.objects[key] == b"payload"
    assert list_remote(config, client=client) == [key]


def test_list_remote_paginates() -> None:
    client = FakeS3(page_size=2)
    config = _config()
    for i in range(5):
        client.objects[f"snapshots/ifta-data-2026081{i}T000000Z.tar.gz"] = b"x"
    assert len(list_remote(config, client=client)) == 5


def test_list_remote_ignores_foreign_objects() -> None:
    client = FakeS3()
    config = _config()
    client.objects["snapshots/ifta-data-20260810T000000Z.tar.gz"] = b"x"
    client.objects["snapshots/notes.txt"] = b"x"
    client.objects["other/ifta-data-20260810T000000Z.tar.gz"] = b"x"
    assert list_remote(config, client=client) == ["snapshots/ifta-data-20260810T000000Z.tar.gz"]


def test_prune_remote_keeps_newest_n() -> None:
    client = FakeS3()
    config = _config()
    for i in range(5):
        client.objects[f"snapshots/ifta-data-2026081{i}T000000Z.tar.gz"] = b"x"

    removed = prune_remote(config, keep=3, client=client)

    assert len(removed) == 2
    remaining = list_remote(config, client=client)
    assert remaining == [
        "snapshots/ifta-data-20260812T000000Z.tar.gz",
        "snapshots/ifta-data-20260813T000000Z.tar.gz",
        "snapshots/ifta-data-20260814T000000Z.tar.gz",
    ]


def test_prune_remote_noop_when_under_limit() -> None:
    client = FakeS3()
    config = _config()
    client.objects["snapshots/ifta-data-20260810T000000Z.tar.gz"] = b"x"
    assert prune_remote(config, keep=3, client=client) == []
    assert client.deleted == []


def test_download_snapshot_writes_local_file(tmp_path) -> None:
    client = FakeS3()
    config = _config()
    key = "snapshots/ifta-data-20260810T000000Z.tar.gz"
    client.objects[key] = b"archive-bytes"

    local = download_snapshot(key, tmp_path / "restore", config, client=client)

    assert local.name == "ifta-data-20260810T000000Z.tar.gz"
    assert local.read_bytes() == b"archive-bytes"


# --- retention resolution --------------------------------------------------


def test_keep_defaults_to_three(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DEFAULT_KEEP == 3
    assert resolve_keep() == 3
    monkeypatch.setenv("IFTA_BACKUP_KEEP", "2")
    assert resolve_keep() == 2
    assert resolve_keep(5) == 5  # explicit wins over env


def test_bad_keep_env_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IFTA_BACKUP_KEEP", "lots")
    with pytest.raises(RuntimeError, match="must be an integer"):
        resolve_keep()


# --- Postgres capture ------------------------------------------------------


def test_snapshot_includes_pg_dump_when_db_url_is_set(tmp_path, monkeypatch) -> None:
    """Job state lives in Postgres, so the archive must carry a dump of it."""
    root = tmp_path / "proj"
    _make_data(root)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # pg_dump writes to the --file= path; emulate that.
        dest = next(a.split("=", 1)[1] for a in cmd if a.startswith("--file="))
        with open(dest, "wb") as fh:
            fh.write(b"PGDMP-fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup_mod.subprocess, "run", fake_run)

    snap = backup_mod.backup_data(
        root, tmp_path / "b", keep=3, db_url="postgresql://ifta:pw@postgres:5432/ifta"
    )

    assert calls and calls[0][0] == "pg_dump"
    assert "--format=custom" in calls[0]
    # The password is stripped out of argv and handed over via PGPASSWORD —
    # see test_audit_followup_aug2026.
    assert calls[0][-1] == "postgresql://ifta@postgres:5432/ifta"
    with tarfile.open(snap) as tar:
        names = tar.getnames()
    assert "data/web_jobs.dump" in names
    assert "data/clients/x.json" in names


def test_pg_dump_failure_aborts_the_backup(tmp_path, monkeypatch) -> None:
    """A partial archive that silently lacks the DB is worse than no archive."""
    root = tmp_path / "proj"
    _make_data(root)

    def failing_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "could not connect to server")

    monkeypatch.setattr(backup_mod.subprocess, "run", failing_run)

    with pytest.raises(RuntimeError, match="could not connect to server"):
        backup_mod.backup_data(root, tmp_path / "b", db_url="postgresql://x/y")


def test_missing_pg_dump_binary_explains_itself(tmp_path, monkeypatch) -> None:
    root = tmp_path / "proj"
    _make_data(root)

    def missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(backup_mod.subprocess, "run", missing)

    with pytest.raises(RuntimeError, match="pg_dump is not on PATH"):
        backup_mod.backup_data(root, tmp_path / "b", db_url="postgresql://x/y")


# --- run_backup orchestration ----------------------------------------------


def test_run_backup_local_only_when_remote_unconfigured(tmp_path) -> None:
    root = tmp_path / "proj"
    _make_data(root)

    result = run_backup(root, tmp_path / "b")

    assert isinstance(result, BackupResult)
    assert result.snapshot.exists()
    assert result.remote_key is None
    assert result.included_pg_dump is False
    assert result.local_kept == 1


def test_run_backup_uploads_and_prunes_both_sides(tmp_path, monkeypatch) -> None:
    root = tmp_path / "proj"
    _make_data(root)
    dest = tmp_path / "b"
    dest.mkdir()
    client = FakeS3()
    config = _config()

    # Four pre-existing snapshots on each side; keep=3 means the oldest goes.
    for i in range(4):
        name = f"ifta-data-2020010{i}T000000Z.tar.gz"
        (dest / name).write_text("old")
        client.objects[f"snapshots/{name}"] = b"old"

    monkeypatch.setattr(
        backup_mod, "upload_snapshot", lambda snap, cfg: upload_snapshot(snap, cfg, client=client)
    )
    monkeypatch.setattr(
        backup_mod, "prune_remote", lambda cfg, *, keep: prune_remote(cfg, keep=keep, client=client)
    )

    result = run_backup(root, dest, keep=3, remote=config)

    assert result.local_kept == 3
    assert result.remote_key is not None
    remaining = list_remote(config, client=client)
    assert len(remaining) == 3
    # The snapshot just taken survived the prune; the oldest did not.
    assert result.remote_key in remaining
    assert "snapshots/ifta-data-20200100T000000Z.tar.gz" not in remaining


def test_run_backup_honours_no_replicate(tmp_path, monkeypatch) -> None:
    """`--no-remote` must not upload even with credentials in the environment."""
    for name, value in R2_ENV.items():
        monkeypatch.setenv(name, value)
    root = tmp_path / "proj"
    _make_data(root)

    def explode(*args, **kwargs):  # pragma: no cover - asserts it is never called
        raise AssertionError("upload attempted despite replicate=False")

    monkeypatch.setattr(backup_mod, "upload_snapshot", explode)

    result = run_backup(root, tmp_path / "b", replicate=False)

    assert result.remote_key is None


def test_run_backup_picks_up_db_url_from_environment(tmp_path, monkeypatch) -> None:
    """The timer runs `ifta backup` with no flags; the DSN comes from the env."""
    root = tmp_path / "proj"
    _make_data(root)
    monkeypatch.setenv("IFTA_WEB_DB_URL", "postgresql://ifta:pw@postgres:5432/ifta")

    def fake_run(cmd, **kwargs):
        dest = next(a.split("=", 1)[1] for a in cmd if a.startswith("--file="))
        with open(dest, "wb") as fh:
            fh.write(b"PGDMP-fake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(backup_mod.subprocess, "run", fake_run)

    result = run_backup(root, tmp_path / "b")

    assert result.included_pg_dump is True
