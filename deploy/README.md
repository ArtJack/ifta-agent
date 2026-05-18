# IFTA web intake — Mac mini deploy

This is the runbook for going live with the self-service IFTA upload flow.
Six steps; the order matters because later steps depend on credentials
created in earlier ones.

| # | What | Where | Time |
|---|---|---|---|
| 1 | Cloudflare Tunnel | Mac mini + Cloudflare dashboard | 20 min |
| 2 | Resend domain | Resend dashboard + Cloudflare DNS | 15 min |
| 3 | Cloudflare Turnstile site key | Cloudflare dashboard | 5 min |
| 4 | `.env` on the Mac mini | Mac mini terminal | 5 min |
| 5 | launchd services | Mac mini terminal | 2 min |
| 6 | Vercel env vars + smoke test | Vercel dashboard + browser | 15 min |

---

## 1. Cloudflare Tunnel — `ifta-api.artjeck.com`

The Mac mini doesn't need a public IP. Cloudflare runs an outbound TLS
tunnel; you point a subdomain at it.

```bash
brew install cloudflared
cloudflared tunnel login                    # opens browser; pick artjeck.com
cloudflared tunnel create ifta-api          # creates tunnel + credentials
cloudflared tunnel route dns ifta-api ifta-api.artjeck.com
```

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: ifta-api
credentials-file: /Users/artjack/.cloudflared/<TUNNEL_UUID>.json
ingress:
  - hostname: ifta-api.artjeck.com
    service: http://localhost:8000
  - service: http_status:404
```

(The UUID is in the JSON file `cloudflared tunnel create` just printed.)

Install as a system service so it survives reboots:

```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

Verify:

```bash
curl -i https://ifta-api.artjeck.com/healthz
# expect 502 for now — backend isn't running yet. That's fine.
```

---

## 2. Resend — DKIM for `artjeck.com`

Confirmation, packet, and failure emails all originate from
`ifta@artjeck.com`. Resend needs DKIM/SPF on the domain.

1. Resend dashboard → **Domains** → **Add Domain** → `artjeck.com`
2. Resend shows 3 DNS records (MX, TXT, CNAME). In Cloudflare DNS, **uncheck "Proxy" (the orange cloud)** for the DKIM CNAME record — proxied DKIM doesn't verify.
3. Wait ~5 min, click **Verify** in Resend. Status should flip to *Verified*.
4. Copy your API key (Settings → API Keys → Create) — starts with `re_`.

---

## 3. Cloudflare Turnstile

1. Cloudflare dashboard → **Turnstile** → **Add Site**
2. Domain: `artjeck.com`
3. Widget mode: **Managed** (auto-detects bots, usually invisible to humans)
4. Save two values:
   - **Site Key** (public, starts with `0x4...`) — for the Next.js frontend
   - **Secret Key** (private) — for the Mac mini backend

---

## 4. `.env` on the Mac mini

On the Mac mini, in `~/Desktop/AI/ifta-agent/.env`:

```bash
# Existing
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_USER_IDS=...

# Web intake — new
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=ArtJeck IFTA <ifta@artjeck.com>
IFTA_WEB_PUBLIC_BASE_URL=https://ifta-api.artjeck.com
IFTA_WEB_ADMIN_BCC=eugene@artjeck.com
TURNSTILE_SECRET_KEY=0x4AAA...        # the SECRET key from step 3
IFTA_WEB_SUBMIT_RATE_LIMIT=3/hour     # optional override
IFTA_WEB_CORS_ORIGINS=https://artjeck.com,https://www.artjeck.com
```

Verify the venv exists and packages are installed:

```bash
cd ~/Desktop/AI/ifta-agent
python3.12 -m venv .venv               # if not already
.venv/bin/pip install -e ".[dev]"
.venv/bin/ifta --help                  # smoke test
.venv/bin/pytest                       # 167+ tests should pass
```

---

## 5. launchd services

```bash
cd ~/Desktop/AI/ifta-agent
bash deploy/install.sh
```

The script renders the plist templates, copies them to
`~/Library/LaunchAgents/`, loads them with launchctl, and prints the PIDs.

Check the logs:

```bash
tail -f logs/web.out.log logs/web.err.log
tail -f logs/worker.out.log logs/worker.err.log
```

Health check (proves both the local app and the Cloudflare tunnel work):

```bash
curl -s http://127.0.0.1:8000/healthz             # local
curl -s https://ifta-api.artjeck.com/healthz      # via tunnel
```

To uninstall: `bash deploy/install.sh uninstall`.

---

## 6. Vercel env + smoke test

In the Vercel dashboard for `artjeck-technology`, add:

| Name | Value | Environments |
|---|---|---|
| `NEXT_PUBLIC_IFTA_API_URL` | `https://ifta-api.artjeck.com` | Production, Preview |
| `NEXT_PUBLIC_TURNSTILE_SITE_KEY` | the **site** key from step 3 | Production, Preview |

Trigger a redeploy (push any commit, or use Vercel's "Redeploy" button).

End-to-end smoke test:

1. Open https://artjeck.com/ifta/submit in a fresh tab
2. Fill in your own email + a recent quarter (e.g. `Q1-2026`)
3. Upload two CSVs (real data or the project's `inbox/Q4-2025/menshikov_miles_and_fuel.csv` twice)
4. Complete the Turnstile widget
5. Click **Upload and process** → form shows "Check your inbox"
6. Click the confirmation link in the email → browser shows "Got it — processing started"
7. ~30 seconds later, packet email arrives with portal CSV + per-truck Excels

Tail the worker log while you wait:

```bash
tail -f logs/worker.out.log
```

You should see:
```
INFO processing submission <uuid> (quarter=Q1-2026)
INFO submission <uuid> done — outputs at .../web_submissions/<uuid>/outputs/...
INFO sent 'Your Q1-2026 IFTA packet' to your@email.com (id=...)
```

---

## Troubleshooting

**Healthz works locally but not via tunnel.** `cloudflared` isn't running.
`sudo launchctl list | grep cloudflared`. Restart:
`sudo launchctl kickstart -k system/com.cloudflare.cloudflared`.

**Form submits 502 from artjeck.com.** Same as above — tunnel is down.

**Form submits but no email arrives.** Tail `logs/worker.out.log` for
Resend errors. Most common: the *from* address domain isn't verified yet
(step 2).

**Worker can't import ifta.** Activate the venv and confirm pytest works
first. The launchd plist runs `.venv/bin/ifta` directly — if it fails
manually, it'll fail under launchd too.

**CAPTCHA always fails.** Site key and secret key are paired — make sure
the site key in Vercel env matches the same Turnstile widget whose secret
key is in the Mac mini's `.env`.

**Submissions piling up in PENDING_CONFIRMATION.** Customers aren't
clicking the link. Inspect with:

```bash
sqlite3 data/web_jobs.db \
  "SELECT id, email, quarter, status, created_at FROM submissions ORDER BY created_at DESC LIMIT 10"
```
