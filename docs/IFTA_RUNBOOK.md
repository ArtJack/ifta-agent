# IFTA Agent — Operations Runbook

The service runs on the Mac mini: a FastAPI web intake + a worker (launchd, kept
alive) and a Telegram bot, with customer state in `data/`. This is the operator's
short guide to keeping it healthy, backed up, and recoverable.

---

## Daily / filing-day checklist

```bash
# 1. Is the service up?
curl -s http://127.0.0.1:8000/healthz            # expect: ok
curl -s https://ifta-api.artjeck.com/healthz     # public (through the tunnel)

# 2. Was data backed up recently? (newest snapshot should be < 24h old)
ifta backup-list

# 3. Are the quarter's tax rates cached?
ifta rates --quarter Q2-2026

# 4. Anything stuck? Check recent jobs and logs.
tail -n 40 logs/worker.err.log logs/web.err.log

# 5. Before you file: the agent's review must be clean.
ifta review --quarter Q2-2026 --client <client>
```

On filing day specifically: confirm rates are current, run the pipeline + review,
and verify the deterministic filing status is `READY_TO_FILE` (not
`READY_WITH_WARNINGS` / `DO_NOT_FILE`) before touching a government portal.

---

## Backups

`data/` holds everything that can't be regenerated: the job database
(`web_jobs.db`), customer registry (`telegram_access.json`, `clients/`), uploaded
files (`web_submissions/`), and cached rates/regulations. It lives only on the Mac
mini, so it is backed up to the **AI-lab share** — a different machine — nightly.

- **What:** a consistent snapshot of `data/`. The live SQLite DB is copied via the
  online-backup API (safe while the web service is writing); WAL sidecars and
  `.DS_Store` are skipped.
- **Where:** `/Volumes/DISK/AI/ifta-backups/ifta-data-<UTC timestamp>.tar.gz`
  (override with `IFTA_BACKUP_DIR`).
- **When:** daily at 03:30 via the `com.artjeck.ifta-backup` launchd agent.
- **Retention:** newest 14 snapshots (`ifta backup --keep N` to change).

### Install / verify

```bash
bash deploy/install.sh                 # installs web + worker + backup agents
ifta backup                            # take one now (also creates the dir)
ifta backup-list                       # confirm the snapshot is there
tail -f logs/backup.{out,err}.log      # watch the nightly run
```

If the share isn't mounted when the job fires, the backup fails loudly (logged to
`backup.err.log`) and the previous snapshots are untouched.

### ⚠️ Encryption

Snapshots are **plain** `tar.gz` — the live data is plain too, so the real control
is **full-disk encryption on both machines**: FileVault on the Mac mini and
full-disk encryption on the lab box. The archives contain customer PII (addresses,
card last-4, EIN-adjacent data); do not copy them anywhere unencrypted. For true
disaster coverage, add an encrypted off-LAN copy (e.g. an `age`/`gpg`-wrapped
upload) later.

---

## Restore

Restores never overwrite live `data/` directly — extract to a staging dir, verify,
then swap it in.

```bash
# 1. Extract a chosen snapshot into a staging directory.
ifta backup-restore --snapshot /Volumes/DISK/AI/ifta-backups/ifta-data-<ts>.tar.gz
#    -> writes ./data.restored-<ts>/data/

# 2. Verify it (row counts, files present).
sqlite3 data.restored-<ts>/data/web_jobs.db \
  "SELECT status, count(*) FROM submissions GROUP BY status;"

# 3. Stop the services, swap, restart.
bash deploy/install.sh uninstall
mv data data.broken-$(date +%Y%m%dT%H%M%S)
mv data.restored-<ts>/data data
bash deploy/install.sh
curl -s http://127.0.0.1:8000/healthz
```

---

## Disaster recovery (Mac mini lost)

1. Set up a new Mac: clone the repo, `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"`, restore `.env` (Anthropic key, Telegram token) from your password manager.
2. Mount the lab share and restore the latest snapshot (steps above).
3. `bash deploy/install.sh`, re-point the Cloudflare tunnel at the new host, verify `/healthz` public + local.
4. Confirm `ifta backup-list` and that the nightly agent is loaded (`launchctl list | grep ifta`).

The bus factor is the `.env` secrets and the lab-share snapshots — keep both
recoverable independently of the Mac mini.
