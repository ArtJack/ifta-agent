# IFTA Pipeline

Quarterly IFTA filing pipeline for trucking operators, with an
LLM-powered review agent that knows IFTA regulations end-to-end and is
trained on the user's own clients.

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

# Pre-filing review by the AI agent (default: Opus 4.7)
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
| `--model` | `claude-opus-4-7` (default), `claude-sonnet-4-6`, `claude-haiku-4-5` | Opus = most precise. Haiku = cheap/fast Q&A. |
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

Ask the customer to open the bot and send `/id`. Add that numeric ID to the
client registry:

```json
{
  "client_id": "dm_express",
  "telegram_user_ids": [123456789]
}
```

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

```
ifta_pipeline/
├─ pyproject.toml          # package metadata, ruff + mypy + pytest config
├─ README.md
├─ .env                    # API key (gitignored)
├─ .env.example
├─ .vscode/                # editor settings + debug launch configs
│  ├─ settings.json
│  ├─ launch.json
│  └─ extensions.json
├─ src/ifta/
│  ├─ __main__.py          # entry point
│  ├─ cli.py               # Click CLI
│  ├─ models.py            # dataclasses + jurisdiction sets
│  ├─ ingest.py            # CSV/Excel/PDF parsers
│  ├─ rates.py             # IFTA rate-matrix fetcher (iftach.org)
│  ├─ calc.py              # fleet-MPG, taxable-gal, surcharge math
│  ├─ validator.py         # rule-based pre-flight checks
│  ├─ report.py            # Excel + portal-CSV writers
│  └─ agent/
│     ├─ __init__.py       # public API: review / ask / chat_loop
│     ├─ prompts.py        # SYSTEM_PROMPT + REVIEW_PROMPT_TEMPLATE
│     ├─ tools.py          # 14 @beta_tool functions the agent can call
│     └─ runner.py         # SDK invocation, model kwargs, conversation loop
├─ data/
│  ├─ regulations.json     # IFTA knowledge base
│  ├─ rates/<NQ20YY>.csv   # cached IFTA rate matrices
│  ├─ david_history.json   # DM EXPRESS INC quarterly data (active client)
│  ├─ david_profile.json
│  ├─ my_truck_history.json   # MENSHIKOV LLC retired filings (reference)
│  ├─ my_truck_profile.json
│  └─ README.md
├─ scripts/
│  ├─ extract_david.py     # rebuilds david_*.json from David/ folder
│  ├─ extract_my_truck.py  # rebuilds my_truck_*.json from MyTruck/ PDFs
│  └─ README.md
├─ inbox/<quarter>/        # drop raw files here
├─ outputs/<quarter>/      # generated files land here
└─ tests/
   ├─ conftest.py
   ├─ test_q1_2025.py      # historical accuracy check
   └─ test_q4_2025_menshikov.py
```

## The AI agent (Phase 2)

The agent has **14 tools** to ground its answers in real data — your
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
