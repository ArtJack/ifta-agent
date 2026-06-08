# IFTA Agent — quarterly fuel-tax filing pipeline with an LLM review agent

**Status:** in production, filing every quarter for a real carrier (DM Express) ·
**429 automated tests** · penny-accurate regression vs. real filings · ~$0.10 model cost per reviewed filing

> Carriers drop in their messy mileage + fuel-card exports; minutes later they get a
> filing-ready IFTA packet that an LLM agent has reviewed against the regulations and their
> own filing history.

📄 **[Full case study](ifta-portfolio/CASE-STUDY.md)** · 📐 **[Requirements](docs/requirements.md)** · **[Design](docs/design.md)**

### The core idea: math is deterministic, the LLM only reviews
A deterministic pipeline (`ingest → calc → validate → report`) computes everything that lands on
the tax form — fleet MPG, taxable gallons, per-jurisdiction tax + surcharge. An LLM agent
(Anthropic SDK, **18 grounded tools**) then *reviews* that computed return against the rule base,
live rates, and real filing history — it never computes a number, so a carrier can trust it for a
government filing. Regression-tested to the penny; a layered eval harness gates the pipeline
(see the [case study](ifta-portfolio/CASE-STUDY.md)).

## Workflow

```
inbox/<quarter>/           outputs/<quarter>/
├─ <miles raw file>   ───┐  ├─ cleaned_miles.csv
├─ <fuel raw file>    ───┤  ├─ cleaned_fuel.csv
                         ├▶ ├─ ifta_review.xlsx   (review-ready Excel)
                         └▶ ├─ ifta_portal.csv    (gov-portal upload format)
                            └─ review_note.md     (when `ifta review` runs)
```

Raw files may be CSV, Excel, or PDF.

## First-time setup

```bash
cd ifta_pipeline
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Create `.env` at the project root:

```
ANTHROPIC_API_KEY=sk-ant-api03-...
```

Get a key at <https://console.anthropic.com/settings/keys>.

## Daily use

The package installs a real `ifta` console script — no `PYTHONPATH` hack
needed.

```bash
# Compute a quarter
ifta run --quarter Q1-2026

# Pre-filing review by the AI agent (default: Sonnet 4.6)
ifta review --quarter Q1-2026

# One-shot question grounded in your data + IFTA rules
ifta ask --quarter Q1-2026 "Why is California's tax so high?"

# Interactive chat with full tool access
ifta chat

# Telegram intake bot (customers upload raw files, bot returns the packet)
ifta telegram-bot

# Just fetch tax rates for a quarter (cache only)
ifta rates --quarter Q1-2026
```

### Model selection

Each agent command takes `--model` and `--effort`:

| Flag | Choices | Notes |
|---|---|---|
| `--model` | `claude-sonnet-4-6` (default), `claude-opus-4-7`, `claude-haiku-4-5` | Sonnet = normal reviews. Opus = highest-risk reviews. Haiku = cheap/fast Q&A. |
| `--effort` | `low`, `medium` (default), `high`, `xhigh`, `max` | Thinking depth — higher = more thorough/expensive. |
| `--max-tokens` | int | Output ceiling per call. Defaults: review 4096, ask 2048, chat 4096. |

## Telegram Intake Bot

The Telegram bot is the customer-facing intake layer. Customers upload raw
mileage and fuel-card files; the bot saves them into the correct client inbox,
runs preflight, runs the deterministic IFTA pipeline, runs the review agent,
and sends back the customer packet.

### Create the bot token

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot`.
3. Pick a display name, for example `Eugene IFTA Bot`.
4. Pick a username ending in `bot`, for example `eugene_ifta_bot`.
5. BotFather returns a token that looks like `123456789:ABC...`.
6. Paste it into `.env`:

```bash
TELEGRAM_BOT_TOKEN=REPLACE_WITH_BOTFATHER_TOKEN
```

Optional admin IDs:

```bash
TELEGRAM_ADMIN_USER_IDS=123456789
```

Run the bot:

```bash
ifta telegram-bot
```

### Approve a customer

The bot is closed by default: a random Telegram user can see their own `/id`,
but cannot start uploads, list clients, upload files, process returns, or see
your customer registry. The bot also only works in private Telegram chats so
customer files are not handled in groups by accident.

Make sure your own Telegram ID is in `.env`:

```bash
TELEGRAM_ADMIN_USER_IDS=123456789
```

Then the easiest customer flow is:

```text
Customer: /request DM Express
Admin:    /approve 555111222 dm_express
```

When a customer sends `/request [company]`, the bot messages every admin with
their Telegram ID and an `/approve ...` command you can paste back into the bot.
Approvals are saved on the Mac mini in `data/telegram_access.json`, which is
ignored by Git so customer Telegram IDs are not pushed to GitHub.

The older manual method still works too. Ask each customer to open the bot in a
private chat and send `/id`, then add that numeric ID only to that customer's
registry file:

```json
{
  "client_id": "dm_express",
  "telegram_user_ids": [123456789]
}
```

One customer can be attached to one client, or to multiple clients if they
manage several companies. A non-admin user cannot choose or process another
client unless their Telegram ID is listed in that client's `client.json`.

Then the customer can use:

```text
/new Q2-2026
```

They upload CSV, Excel, or PDF files as Telegram documents. When the bot says
preflight is clean, they send:

```text
/process
```

The bot sends back:

- `ifta_portal.csv`
- `ifta_review.xlsx`
- `review_note.md`
- one per-truck Excel file per truck

If current-quarter rates are unavailable, the bot marks the packet as
`REVIEW REQUIRED` and warns not to upload yet.

## Project layout

**44 Python modules · 16k LOC · 429 tests across 38 files.** Layered by responsibility:

```
src/ifta/
├─ cli.py                  # Click CLI: run | review | ask | chat | telegram-bot | rates
├─ models.py               # canonical data model + jurisdiction sets
│  ── deterministic pipeline (the math) ──
├─ ingest.py               # CSV / Excel / PDF parsers
├─ rates.py                # IFTA rate-matrix fetcher + cache (iftach.org)
├─ calc.py                 # fleet-MPG, taxable gallons, surcharge math
├─ validator.py + preflight.py   # rule-based pre-flight checks
├─ report.py               # Excel + portal-CSV writers
├─ agent/                  # ── LLM review layer (judgment) ──
│  ├─ runner.py            # SDK invocation, model/effort kwargs, conversation loop
│  ├─ tools.py             # 18 grounded tools the agent can call
│  ├─ prompts.py           # system + review-prompt templates
│  └─ context.py · metrics.py · tracing.py
├─ intake/                 # extract · receipts (vision) · reconcile · report
├─ eval/                   # runner · judge (validated LLM-as-judge with agreement gate)
└─ web/                    # ── multi-tenant FastAPI service ──
   ├─ app.py · db.py · pipeline.py · worker.py · customer_view.py
   ├─ telegram_approval.py # operator approval gate
   ├─ turnstile.py         # CAPTCHA   email.py  models.py  intake_brief.py

docs/        requirements.md · design.md (SDD) · IFTA_RUNBOOK · BENCHMARK · JUDGE · TRACING
evals/       benchmark history, cases, receipt eval reports (PII receipts git-ignored)
data/        regulations KB · cached rate matrices · per-client history (git-ignored)
ifta-portfolio/CASE-STUDY.md
tests/       38 files, 429 tests — incl. penny-accurate regression vs. real filings
```

## The AI agent (Phase 2)

The agent has **16 tools** to ground its answers in real data — your
returns, the validator, the regulations KB, rate matrix, and 21 quarters
of historical filings between two carriers.

| Tool category | Tools |
|---|---|
| Pipeline | `list_quarters`, `query_return`, `query_findings`, `compare_to_filing` |
| Rules | `lookup_rate`, `get_regulations` |
| DM EXPRESS INC ("David", active) | `get_david_profile`, `query_david_history`, `list_david_files` |
| MENSHIKOV LLC (retired, reference) | `get_my_truck_profile`, `query_my_truck_history`, `compare_quarter_to_history`, `list_past_filings`, `read_past_filing` |

System prompt and review-prompt template live in
`src/ifta/agent/prompts.py` — easy to edit without touching code.

## Validating the math

```bash
.venv/bin/pytest
```

Two regression tests confirm fleet MPG, miles, and total tax due match
known-correct historical filings to the penny.

## Sign convention

All tax outputs use the IFTA standard: positive Tax Due = you owe the
state, negative = state owes a credit. Matches CDTFA and KY DOR portal
behavior.

## Re-extracting historical data

When you add new historical PDFs/xlsx files (e.g. David sends his Q2
2026 sheet later):

```bash
.venv/bin/python scripts/extract_david.py
.venv/bin/python scripts/extract_my_truck.py
```
