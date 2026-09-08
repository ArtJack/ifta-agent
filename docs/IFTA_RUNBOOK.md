# IFTA Agent — Operations Runbook

Production is one Oracle Cloud VM (`artjeck-oracle`, Ubuntu 24.04) running the
whole stack under Docker Compose: `web` (FastAPI intake), `worker`, `telegram-bot`,
`postgres`, and `cloudflared`. systemd owns the stack's lifecycle via
`ifta.service`; Docker restarts individual containers. Customer state lives in
`/var/lib/ifta` on the host and reaches the containers as bind mounts.

The repo is at `/opt/ifta-agent`, owned by `ubuntu`. Reach the box with
`ssh oracle`. Deployment details are in [ORACLE.md](ORACLE.md); this is the
operator's short guide to keeping it healthy, backed up, and recoverable.

> The Mac mini ran this service until September 2026 and no longer does — its
> launchd agents are retired. A Mac checkout is a development clone: its `data/`,
> `logs/` and any local snapshots are stale and say nothing about production.

---

## Daily / filing-day checklist

```bash
# One command, from a dev checkout: health, backups, rates, logs, smoke tests.
ifta qa-filing-day --quarter Q3-2026 --client <client>
```

It writes a dated evidence report under `data/qa/`. A `FAIL` means do not file
until it is resolved. Checks it cannot answer from your laptop (the server's
backups and logs) report `WARN` and name the command to run on the box — they are
not silently counted as passing.

Run individually if you want to see them:

```bash
# 1. Is the service up?
curl -s https://ifta-api.artjeck.com/healthz      # public, through the tunnel
ssh oracle 'sudo docker compose -f /opt/ifta-agent/deploy/oracle/docker-compose.yml ps'

# 2. Was data backed up recently? (newest snapshot should be < 24h old)
ssh oracle 'sudo ls -lt /var/lib/ifta/backups | head'
ssh oracle 'sudo restic snapshots --tag ifta | tail -3'   # the off-box copy

# 3. Are the quarter's tax rates cached, and are they really this quarter's?
ssh oracle 'sudo docker compose -f /opt/ifta-agent/deploy/oracle/docker-compose.yml \
    exec -T web ifta rates --quarter Q3-2026'

# 4. Anything stuck?
ssh oracle 'sudo journalctl -u ifta --since -1d --no-pager | tail -40'
ssh oracle 'sudo docker compose -f /opt/ifta-agent/deploy/oracle/docker-compose.yml \
    logs --tail 40 worker web'

# 5. Before you file: the review must be clean.
ssh oracle 'sudo docker compose -f /opt/ifta-agent/deploy/oracle/docker-compose.yml \
    exec -T web ifta review --quarter Q3-2026 --client <client>'
```

On filing day: confirm the rates are the **current** quarter's (a fallback to a
prior quarter is itself a blocker), run the pipeline + review, and verify the
deterministic filing status is `READY_TO_FILE` before touching a government
portal. `READY_WITH_WARNINGS` means read the warnings first; `DO_NOT_FILE` means
what it says — a missing rate or a missing KY/VA surcharge line understates the
return.

---

## Deploying a change

```bash
ssh oracle
sudo bash /opt/ifta-agent/deploy/oracle/update.sh      # snapshot, pull, rebuild, roll
```

It refuses to deploy if the pre-deploy snapshot fails. To roll back, tag the
running image *before* deploying (`sudo docker tag ifta:latest ifta:pre-$(date +%F)`),
since the script prunes dangling images at the end.

---

## Backups

`/var/lib/ifta` holds everything that cannot be regenerated: customer registry
(`state/telegram_access.json`, `clients/`), uploaded files (`web_submissions/`),
agent traces, and the Postgres job database.

- **What:** a `tar.gz` of the mounted data plus a `pg_dump --format=custom` of the
  job database. Job state is *not* under `data/`, so a file-only archive would
  restore with zero submissions.
- **When:** nightly at 03:30 (+ up to 5 min jitter) via `ifta-backup.timer`.
- **Where, on the box:** `/var/lib/ifta/backups`, newest `IFTA_BACKUP_KEEP` (3) kept.
- **Where, off the box:** the host-level `offsite-backup.timer` runs restic daily
  at ~04:33 and replicates `/var/lib/ifta/backups` to Cloudflare R2. **This is the
  disaster copy** — the app's own `IFTA_BACKUP_R2_*` replication is not configured,
  so every nightly run logs `R2 not configured — this snapshot never left the box`.
  That line is expected; restic is what carries it off-box.

```bash
ssh oracle 'sudo systemctl start ifta-backup'          # take one now
ssh oracle 'sudo journalctl -u ifta-backup -n 20 --no-pager'
ssh oracle 'sudo restic snapshots --tag ifta | tail'   # prove the off-box copy exists
```

### ⚠️ Encryption

Snapshots on the box are **plain** `tar.gz` — as is the live data — so the control
is disk encryption on the VM plus a **private** R2 bucket; restic encrypts what it
stores there. The archives contain customer PII; do not copy them anywhere
unencrypted. Losing the restic password means losing every off-box backup: keep it
in the password manager.

---

## Restore

Restores never overwrite live data directly — extract to a staging dir, verify,
then swap it in. See the drill below; run it monthly and before any hosting change.

```bash
# 1. Choose a snapshot (local, or fetched back from R2 with restic).
ssh oracle 'sudo ls -1t /var/lib/ifta/backups/ifta-data-*.tar.gz | head'

# 2. Extract into throwaway staging, never over live data.
ssh oracle 'sudo docker compose -f /opt/ifta-agent/deploy/oracle/docker-compose.yml \
    run --rm -v /var/lib/ifta/backups:/backups:ro backup \
    ifta backup-restore --snapshot /backups/<name>.tar.gz --into /tmp/ifta-restore'

# 3. Verify. Job state is a Postgres dump, not SQLite.
ssh oracle 'pg_restore --list /tmp/ifta-restore/data/web_jobs.dump | grep -c submissions'
ssh oracle 'ls /tmp/ifta-restore/data/state/telegram_access.json /tmp/ifta-restore/data/clients'

# 4. Only if you are really restoring: stop, swap, start.
ssh oracle 'sudo systemctl stop ifta'
#    move /var/lib/ifta/<dir> aside, put the restored copy in place, chown -R 10001:10001
ssh oracle 'sudo systemctl start ifta'
curl -s https://ifta-api.artjeck.com/healthz
```

---

## Disaster recovery (the box is lost)

1. New VM, Ubuntu 24.04. `git clone git@github.com:ArtJack/ifta-agent /opt/ifta-agent`
   **as the deploy user, not root** — git auth is per-user and `sudo git clone`
   fails with a misleading host-key error.
2. Restore secrets: `sops -d --input-type dotenv --output-type dotenv \
   deploy/oracle/.env.sops > deploy/oracle/.env && chmod 600 deploy/oracle/.env`.
   This needs the age private key from the password manager — without it every
   `.sops` file in the estate is unrecoverable.
3. `sudo bash deploy/oracle/install.sh` (installs Docker, creates the uid-10001
   state directories, installs the systemd units).
4. Restore the newest snapshot from R2 with restic into `/var/lib/ifta`, then
   `chown -R 10001:10001`.
5. Point the `ifta-api` Cloudflare tunnel at the new host (the DNS CNAME already
   targets the tunnel UUID, so DNS itself does not change), and set
   `COMPOSE_PROFILES=tunnel` in `.env`.
6. Verify: `/healthz` returns ok publicly, `/docs` returns 404, `ifta qa-filing-day`
   is clean, and `systemctl list-timers ifta-backup` shows the nightly job armed.

The bus factor is the **age private key** and the **restic password**. Keep both
recoverable independently of this box.
