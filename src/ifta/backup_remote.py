"""Off-box replication of data snapshots to Cloudflare R2 (S3-compatible).

The Oracle box holds the only live copy of customer state, so a nightly
snapshot that never leaves the box is not a backup. This module pushes each
``ifta-data-<ts>.tar.gz`` to an R2 bucket and prunes the remote to the newest
``keep`` objects — the same retention rule as the local directory, so the
bucket never accumulates more than a handful of archives.

Configuration is entirely environment-driven (see ``RemoteConfig.from_env``).
When *nothing* is set the caller gets ``None`` and backups stay local-only;
when the config is only *partially* set that is a misconfiguration and we
raise, because silently skipping replication is precisely how a backup turns
out to be missing on the day it is needed.

These archives are plain (unencrypted) tarballs of real customer PII. The R2
bucket MUST be private — no public access, no custom public domain. See
``docs/ORACLE.md``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Object keys mirror the local filenames, so `ifta-data-*.tar.gz` sorts
# chronologically as a string (UTC timestamps, fixed width).
SNAPSHOT_SUFFIX = ".tar.gz"
SNAPSHOT_STEM = "ifta-data-"

_ENV_BUCKET = "IFTA_BACKUP_R2_BUCKET"
_ENV_ENDPOINT = "IFTA_BACKUP_R2_ENDPOINT"
_ENV_KEY_ID = "IFTA_BACKUP_R2_ACCESS_KEY_ID"
_ENV_SECRET = "IFTA_BACKUP_R2_SECRET_ACCESS_KEY"
_ENV_PREFIX = "IFTA_BACKUP_R2_PREFIX"

_REQUIRED = (_ENV_BUCKET, _ENV_ENDPOINT, _ENV_KEY_ID, _ENV_SECRET)


@dataclass(frozen=True)
class RemoteConfig:
    """Connection details for the R2 bucket that mirrors local snapshots."""

    bucket: str
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    prefix: str = "snapshots"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RemoteConfig | None:
        """Build a config from the environment, or ``None`` when unconfigured.

        Raises ``RuntimeError`` when *some* but not all of the required
        variables are present — a half-configured remote means backups are
        silently not being replicated, which we refuse to do quietly.
        """
        src = os.environ if env is None else env
        present = [name for name in _REQUIRED if src.get(name, "").strip()]
        if not present:
            return None
        if len(present) != len(_REQUIRED):
            missing = ", ".join(sorted(set(_REQUIRED) - set(present)))
            raise RuntimeError(
                f"Cloudflare R2 backup is partially configured — missing: {missing}. "
                f"Set all of {', '.join(_REQUIRED)}, or none of them to keep backups local-only."
            )
        return cls(
            bucket=src[_ENV_BUCKET].strip(),
            endpoint_url=src[_ENV_ENDPOINT].strip().rstrip("/"),
            access_key_id=src[_ENV_KEY_ID].strip(),
            secret_access_key=src[_ENV_SECRET].strip(),
            prefix=(src.get(_ENV_PREFIX) or "snapshots").strip().strip("/"),
        )

    def key_for(self, name: str) -> str:
        """Object key for a snapshot filename."""
        return f"{self.prefix}/{name}" if self.prefix else name


def build_client(config: RemoteConfig) -> Any:
    """Create a boto3 S3 client pointed at R2.

    Two R2-specific details that are easy to get wrong:

    * ``region_name`` must be the literal ``"auto"`` — R2 has no regions, and
      botocore refuses to sign without *some* region.
    * boto3 >= 1.36 adds integrity checksums to every upload by default, which
      S3-compatible providers have historically rejected. Requesting checksums
      only ``when_required`` keeps uploads working across boto3 versions.
    """
    try:
        import boto3
        from botocore.config import Config
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "Cloudflare R2 backup needs boto3 — install the extra: pip install -e '.[oracle]'"
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_supported",
        ),
    )


def upload_snapshot(snapshot: Path, config: RemoteConfig, client: Any | None = None) -> str:
    """Upload one snapshot and return its object key."""
    api = build_client(config) if client is None else client
    key = config.key_for(snapshot.name)
    api.upload_file(str(snapshot), config.bucket, key)
    return key


def list_remote(config: RemoteConfig, client: Any | None = None) -> list[str]:
    """All snapshot keys in the bucket under the configured prefix, oldest first."""
    api = build_client(config) if client is None else client
    prefix = f"{config.prefix}/{SNAPSHOT_STEM}" if config.prefix else SNAPSHOT_STEM
    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": config.bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = api.list_objects_v2(**kwargs)
        keys.extend(
            item["Key"]
            for item in page.get("Contents", [])
            if item["Key"].endswith(SNAPSHOT_SUFFIX)
        )
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            break
    return sorted(keys)


def prune_remote(config: RemoteConfig, *, keep: int, client: Any | None = None) -> list[str]:
    """Delete all but the newest ``keep`` remote snapshots. Returns removed keys."""
    if keep <= 0:
        return []
    api = build_client(config) if client is None else client
    keys = list_remote(config, client=api)
    stale = keys[:-keep]
    if stale:
        api.delete_objects(
            Bucket=config.bucket,
            Delete={"Objects": [{"Key": key} for key in stale], "Quiet": True},
        )
    return stale


def download_snapshot(
    key: str, into: Path, config: RemoteConfig, client: Any | None = None
) -> Path:
    """Fetch one remote snapshot into ``into`` (a directory). Returns the local path."""
    api = build_client(config) if client is None else client
    into.mkdir(parents=True, exist_ok=True)
    local = into / Path(key).name
    api.download_file(config.bucket, key, str(local))
    return local
