# IFTA on the Oracle Linux box — migration + deployment runbook

The permanent home for the IFTA pipeline: one Oracle Linux server running the
whole stack under Docker Compose, reached through a Cloudflare Tunnel, with
nightly snapshots replicated to Cloudflare R2.

This replaces the Azure Container Apps deployment (`docs/AZURE.md`), whose
credit expires **2026-10-09**, and the Mac mini launchd deployment
(`deploy/README.md`). Both remain documented as fallbacks.

> **Real customer PII.** `data/clients/`, `data/web_submissions/` and the
> backup archives contain real carrier data. Keep the box's disk encrypted,
> keep the R2 bucket private, and keep `deploy/oracle/.env` at mode 0600.

---

## Architecture

```mermaid
flowchart LR
    U[Customer<br/>artjeck.com/ifta/submit] -->|HTTPS| CF[Cloudflare edge]
    CF -->|tunnel, outbound only| CFD[cloudflared]

    subgraph BOX["Oracle Linux box — docker compose"]
        CFD --> WEB[web<br/>FastAPI :8000]
        WEB --> PG[(postgres 16)]
        WRK[worker] --> PG
        TG[telegram-bot] --> PG
        WEB -.shared bind mounts.- FS[/var/lib/ifta<br/>clients · submissions · traces · state/]
        WRK -.-> FS
        TG -.-> FS
        BK[backup<br/>systemd timer 03:30] --> FS
        BK --> PG
    end

    WRK -->|API| AN[Anthropic]
    WRK -->|email| RS[Resend]
    BK -->|nightly, keep 3| R2[(Cloudflare R2)]
```

**No inbound ports.** `cloudflared` dials *out* to Cloudflare, so the OCI
security list / firewalld needs no open ports at all. This is the single
biggest reason to prefer the tunnel over a public IP plus Caddy.

| Piece | Choice | Why |
|---|---|---|
| Orchestration | Docker Compose + systemd | One box; systemd owns start/stop, Docker owns per-container restarts |
| Job state | Postgres 16 container | Reuses the `db_postgres` backend already built and tested for Azure |
| PII storage | Bind mounts under `/var/lib/ifta` | Inspectable and migratable from the host; `:Z` labels satisfy SELinux |
| Rate cache | Named volume `ifta-rates` | Docker seeds a named volume from the image; a bind mount would hide the committed cache |
| Ingress | Cloudflare Tunnel | No open ports, TLS at the edge, reuses the existing domain |
| Backups | Nightly tar.gz → R2, keep 3 | Off-box copy; each run deletes the oldest beyond 3 |

---

## Prerequisites

- Oracle Linux 8 or 9, root/sudo, outbound internet (no inbound needed).
- The repo cloned on the box, e.g. `/opt/ifta-agent`.
- A Cloudflare account holding `artjeck.com` (already the case).
- Keys in hand: Anthropic, Resend, Turnstile secret, Telegram bot token.

```bash
sudo dnf -y install git
sudo git clone https://github.com/ArtJack/ifta-agent /opt/ifta-agent
```

Everything below assumes `PROJECT=/opt/ifta-agent`.

---

## 1. Cloudflare Tunnel

Create a **remotely-managed** tunnel — ingress lives in the dashboard, so there
is no credentials file to place on the box.

1. Cloudflare **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel**
   → type **Cloudflared** → name it `ifta-oracle`.
2. Copy the **tunnel token** from the install command it shows. Do *not* run
   that command; compose runs cloudflared for you.
3. **Public Hostname** tab → **Add a public hostname**:
   - Subdomain `ifta-api`, domain `artjeck.com`
   - Service: **HTTP** → `web:8000`
     (`web` is the compose service name — cloudflared resolves it on the
     compose network, which is why no port is published to the host.)

Leave the old Mac mini / Azure tunnel running for now; DNS still points there
until step 6.

---

## 2. Cloudflare R2 bucket

1. Cloudflare dashboard → **R2** → **Create bucket** → `ifta-backups`.
   Location hint: closest to the box. **Do not** attach a public domain or
   enable public access — these archives are unencrypted PII.
2. **Manage API tokens** → **Create API token**:
   - Permission **Object Read & Write**
   - Scope it to the `ifta-backups` bucket only
3. Save the **Access Key ID**, **Secret Access Key**, and the
   **S3 endpoint** (`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`).

Free tier is 10 GB-month of storage with no egress fees. Three snapshots of the
current dataset are far under that — check yours with `du -sh data/` before
cutover, and see [If the data outgrows R2](#if-the-data-outgrows-r2) if it is
ever close.

---

## 3. Configure secrets

```bash
cd /opt/ifta-agent
sudo install -m 600 deploy/oracle/.env.example deploy/oracle/.env
sudo openssl rand -base64 32     # -> POSTGRES_PASSWORD
sudo openssl rand -hex 32        # -> IFTA_WEB_BACKEND_KEY
sudo $EDITOR deploy/oracle/.env
```

Fill in every `REPLACE*` value. `deploy/oracle/.env.example` documents each
one. The install script refuses to proceed while `POSTGRES_PASSWORD`,
`ANTHROPIC_API_KEY`, `RESEND_API_KEY`, or `CLOUDFLARE_TUNNEL_TOKEN` is still a
placeholder.

Leave `TELEGRAM_BOT_TOKEN` empty for now — the bot only starts under the
`telegram` profile (step 7).

---

## 4. Install

```bash
sudo bash deploy/oracle/install.sh
```

It installs Docker CE and the compose plugin (Oracle Linux ships podman;
the unit files drive `docker compose`), creates `/var/lib/ifta/{clients,
web_submissions,traces,state,postgres}` owned by uid 10001, registers an
SELinux `container_file_t` context for that tree, installs the systemd units,
then builds the image and starts the stack.

First build takes several minutes. Watch it:

```bash
journalctl -u ifta -f
```

Verify:

```bash
cd /opt/ifta-agent/deploy/oracle
sudo docker compose ps                                   # all Up / healthy
sudo docker compose exec web curl -fsS localhost:8000/healthz   # -> ok
sudo bash install.sh doctor                              # full diagnostic
```

---

## 5. Migrate the data

Two things move: **carrier history** (the real asset) and **Telegram
approvals**. Job state does *not* — see the note at the end of this section.

### From the Mac mini

```bash
# On the Mac mini
cd ~/Desktop/AI/ifta-agent
.venv/bin/ifta backup --dest /tmp/cutover --keep 3
scp /tmp/cutover/ifta-data-*.tar.gz oracle-box:/tmp/
```

```bash
# On the Oracle box
cd /tmp && tar xzf ifta-data-*.tar.gz
sudo systemctl stop ifta
sudo rsync -a data/clients/            /var/lib/ifta/clients/
sudo rsync -a data/web_submissions/    /var/lib/ifta/web_submissions/ 2>/dev/null || true
sudo cp data/telegram_access.json      /var/lib/ifta/state/telegram_access.json
sudo chown -R 10001:10001 /var/lib/ifta
sudo restorecon -R /var/lib/ifta
sudo systemctl start ifta
```

### From Azure

```bash
# Pull the Azure Files shares down (storage account name from the deployment outputs)
az storage file download-batch --account-name <ACCT> -s clients -d ./clients
az storage file download-batch --account-name <ACCT> -s state   -d ./state
# then the same rsync/chown/restorecon block as above
```

Postgres-to-Postgres, if you want the Azure job rows too:

```bash
pg_dump --format=custom --no-owner --no-privileges \
    "postgresql://USER:PASS@<azure-pg-fqdn>:5432/ifta?sslmode=require" -f azure.dump
sudo docker compose cp azure.dump postgres:/tmp/azure.dump
sudo docker compose exec postgres pg_restore -U ifta -d ifta --clean --if-exists /tmp/azure.dump
```

> **Job state is transient.** The `submissions` table is queue state, not
> filing history — the carrier history the agent reads lives in
> `data/clients/`. Starting fresh on the new box is normal and expected. Just
> let in-flight work drain first: on the old host,
> `SELECT status, COUNT(*) FROM submissions GROUP BY status` should show
> nothing `QUEUED` or `RUNNING`.

Confirm the box sees the history:

```bash
sudo docker compose exec web ifta clients   # lists carriers from data/clients
```

---

## 6. Cut over DNS

Until now `ifta-api.artjeck.com` still resolves to the old host.

1. Cloudflare **DNS** → delete (or rename) the old `ifta-api` CNAME pointing at
   the Mac mini / Azure tunnel.
2. The `ifta-oracle` tunnel's Public Hostname entry from step 1 creates the
   replacement record automatically. Confirm `ifta-api` now points at
   `<tunnel-uuid>.cfargotunnel.com`.

```bash
curl -fsS https://ifta-api.artjeck.com/healthz     # -> ok, served by the Oracle box
```

Vercel env vars stay unchanged — the hostname did not change, only what sits
behind it. Then run the full end-to-end submit flow exactly as in
[`deploy/README.md` step 6](../deploy/README.md): submit → confirmation email →
click link → packet email with portal CSV and per-truck Excels.

Tail the worker while you wait:

```bash
cd /opt/ifta-agent/deploy/oracle && sudo docker compose logs -f worker
```

---

## 7. Enable the Telegram bot

Once the real token is in `.env`:

```bash
cd /opt/ifta-agent/deploy/oracle
sudo docker compose --profile telegram up -d
sudo docker compose logs -f telegram-bot
```

Stop the Mac mini's bot first — two pollers on one token fight over updates and
each sees a random half of the messages.

To make it start on boot with everything else, add `--profile telegram` to the
`ExecStart` line in `/etc/systemd/system/ifta.service`, then
`sudo systemctl daemon-reload`.

---

## Backups

`ifta-backup.timer` fires nightly at **03:30** local (plus up to 5 min jitter)
and runs `ifta backup` in a one-shot container. Each run:

1. Copies `data/` (clients, submissions, traces, state) into a staging dir.
2. `pg_dump --format=custom` of the job database → `data/web_jobs.dump` in the
   same archive. This matters: job state is in Postgres, not under `data/`, so
   a file-only archive would restore with zero submissions.
3. Writes `ifta-data-<UTC timestamp>.tar.gz` to `/var/lib/ifta/backups`.
4. Uploads it to `r2://ifta-backups/snapshots/`.
5. **Prunes both sides to the newest `IFTA_BACKUP_KEEP` (default 3)** — the
   fourth-oldest is deleted as each new one lands, so neither the disk nor the
   bucket grows without bound.

```bash
sudo systemctl start ifta-backup            # run one now
journalctl -u ifta-backup -n 30             # what it did
systemctl list-timers ifta-backup           # when it next fires

cd /opt/ifta-agent/deploy/oracle
sudo docker compose --profile backup run --rm backup ifta backup-list            # local
sudo docker compose --profile backup run --rm backup ifta backup-list --remote   # in R2
```

`Persistent=true` on the timer means a snapshot missed while the box was off
runs at next boot instead of leaving a silent gap.

If R2 is only *partially* configured, the backup **fails loudly** rather than
quietly keeping snapshots on the box — a backup you believe is offsite but
isn't is the worst of both worlds.

### Restore drill

Do this once now, so you have done it before you need it.

```bash
cd /opt/ifta-agent/deploy/oracle
S=backup   # the one-shot service

# 1. Fetch the newest archive out of R2
sudo docker compose --profile backup run --rm $S ifta backup-fetch

# 2. Extract to a staging dir — never over the live data
sudo docker compose --profile backup run --rm $S \
    ifta backup-restore --snapshot /backups/ifta-data-<ts>.tar.gz --into /backups/verify

# 3. Eyeball it
sudo ls /var/lib/ifta/backups/verify/data/clients

# 4. Files: stop, swap, start
sudo systemctl stop ifta
sudo mv /var/lib/ifta/clients /var/lib/ifta/clients.bak
sudo mv /var/lib/ifta/backups/verify/data/clients /var/lib/ifta/clients
sudo chown -R 10001:10001 /var/lib/ifta/clients && sudo restorecon -R /var/lib/ifta
sudo systemctl start ifta

# 5. Database, if you also need the job rows back
sudo docker compose cp /var/lib/ifta/backups/verify/data/web_jobs.dump postgres:/tmp/j.dump
sudo docker compose exec postgres pg_restore -U ifta -d ifta --clean --if-exists /tmp/j.dump
```

The file swap and the database load are deliberately separate steps so each is
independently verifiable.

### If the data outgrows R2

Three snapshots must fit in R2's 10 GB free tier. Check headroom:

```bash
sudo du -sh /var/lib/ifta/backups /var/lib/ifta/web_submissions
```

`web_submissions` is what grows — every customer upload plus generated packet.
Two options when it gets close:

- **Prune old submissions.** They are reproducible from `data/clients/` history
  and already delivered by email; archiving anything older than a year keeps
  the snapshot small.
- **Pull to the Alienware 2 TB SSD instead.** The box cannot push to your home
  LAN, so run this *from* the Alienware on a schedule (Task Scheduler / cron),
  using the same R2 credentials or `rsync` over SSH:

  ```bash
  rclone sync r2:ifta-backups/snapshots /mnt/ssd/ifta-backups --max-age 30d
  ```

  Then set `IFTA_BACKUP_KEEP=2` on the box to shrink the cloud footprint.

---

## Day-to-day operations

```bash
# Deploy new code (takes a pre-deploy snapshot first, then rebuilds and rolls)
sudo bash /opt/ifta-agent/deploy/oracle/update.sh

# Status / logs
systemctl status ifta
journalctl -u ifta -f
cd /opt/ifta-agent/deploy/oracle && sudo docker compose logs -f web worker

# Restart one service
sudo docker compose restart worker

# Full stop / start
sudo systemctl stop ifta
sudo systemctl start ifta

# Diagnose
sudo bash /opt/ifta-agent/deploy/oracle/install.sh doctor
```

`update.sh` refuses to deploy if the pre-deploy snapshot fails — the moment you
need a backup from *before* a deploy is exactly the moment you would regret
skipping it.

---

## Rollback

The old deployments are untouched by this migration, so rollback is a DNS
change:

1. Point `ifta-api.artjeck.com` back at the Mac mini tunnel (or the Azure
   Container App).
2. Restart the old host's services (`bash deploy/install.sh` on the Mac mini).
3. Copy back anything the Oracle box accumulated in the meantime:
   `/var/lib/ifta/clients` → the old host's `data/clients/`.

Keep the Oracle stack running while you confirm — `sudo systemctl stop ifta`
only once you are satisfied.

---

## Decommissioning Azure

Once the Oracle box has run a full quarter-end cleanly, and **after** verifying
a restore drill works:

```bash
# Final export of anything only Azure has
az storage file download-batch --account-name <ACCT> -s clients -d ./azure-final-clients

# Then, one command removes every Azure resource and stops all billing
az group delete --name rg-ifta --yes --no-wait
```

`docs/AZURE.md` stays in the repo as the record of how it was built and as a
migration-back path.

---

## Troubleshooting

**`EACCES` / permission denied writing to `/app/data/...`.** SELinux. The bind
mounts need the `:Z` label (they have it in the compose file) *and* host
ownership by uid 10001:

```bash
sudo chown -R 10001:10001 /var/lib/ifta && sudo restorecon -R /var/lib/ifta
getenforce   # Enforcing is fine — do not disable it
```

**`pg_dump: server version mismatch`.** `pg_dump` must be at least the server's
version. The image pins `postgresql-client-16` and compose pins
`postgres:16-alpine`; if you bump one, bump the `PG_MAJOR` build arg to match.

**Backup fails with `partially configured`.** Some but not all of the four
`IFTA_BACKUP_R2_*` variables are set. Set all four or none.

**R2 upload fails with a checksum or `NotImplemented` error.** Newer boto3 adds
integrity checksums by default. `build_client` already requests them only
`when_required`; if you pinned an unusual boto3, that is the knob.

**`docker: command not found` but podman works.** Oracle Linux ships podman;
the unit files drive `docker compose`. Re-run `install.sh`, which adds Docker's
repo. (Podman would work with `podman-compose`, but the systemd units and the
`:Z` handling here assume Docker.)

**Every customer shares one rate-limit bucket.** `FORWARDED_ALLOW_IPS=*` must
be set (it is, in the compose env) — cloudflared is not loopback, so without it
uvicorn ignores `X-Forwarded-For` and sees one client IP for everyone.

**`/submit` returns 503 "CAPTCHA not configured".** That is the fail-closed
guard doing its job: `IFTA_WEB_REQUIRE_TURNSTILE=1` (the compose default) makes
a missing `TURNSTILE_SECRET_KEY` reject anonymous submissions rather than
silently accepting them. Set the real Turnstile secret. Do **not** turn the
guard off to make the error go away — without it the endpoint is open to
anyone, and every submission spends model tokens and sends mail from your
domain.

**A submission sits in QUEUED for a minute after an error.** Expected. A
transient failure (iftach.org blip, model API hiccup, Postgres failover) is
retried with a back-off — 60s, then 120s — before the customer is told
anything. `next_attempt_at` on the row holds the deadline; `attempts` counts
claims and stops at 3. Previously all three retries fired within a few
milliseconds, so the customer got a failure email for outages that would have
cleared on their own.

**Tunnel is up but `/healthz` 502s.** The Public Hostname service must be
`http://web:8000` — the compose service name, not `localhost:8000`. cloudflared
runs in its own container.

**Telegram bot answers twice, or misses messages.** Two pollers on one token.
Stop the Mac mini's bot.
