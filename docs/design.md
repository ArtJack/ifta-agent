# IFTA Agent — Design (SDD)

> How the system meets [requirements.md](requirements.md). Pairs with the deeper
> [case study](../ifta-portfolio/CASE-STUDY.md).

## 1. Architecture overview
A deliberately split, two-layer system: **deterministic math** + **LLM judgment**, wrapped in a
multi-tenant web/Telegram intake and a layered eval harness.

```
 customer ──upload──▶ artjeck.com/ifta  (Next.js on Vercel)
                            │  server-side proxy (hides backend key)
                            ▼
              ifta-api.artjeck.com  (FastAPI, Cloudflare Tunnel → Mac mini)
                            │
        ┌───────────────────┼─────────────────────────┐
        ▼                   ▼                         ▼
  deterministic        review agent              operator gate
  pipeline             (Claude + 18 tools)       (Telegram approve/reject)
  ingest→calc→         grounded in returns,      → packet emailed (Resend)
  validate→report      rules, rates, history
```

## 2. Layered code structure (`src/ifta/`)
- **Core pipeline (deterministic):** `ingest.py` (CSV/Excel/PDF parsers) → `calc.py` (fleet MPG,
  taxable gallons, surcharge math) → `validator.py` + `preflight.py` (rule checks) →
  `report.py` (Excel + portal CSV). `rates.py` fetches/caches the quarterly rate matrix;
  `models.py` holds the canonical data model + jurisdiction sets.
- **`agent/`** — the LLM review layer: `runner.py` (SDK invocation, model/effort kwargs,
  conversation loop), `tools.py` (18 grounded tools), `prompts.py` (system + review templates),
  `context.py`, `metrics.py`, `tracing.py`.
- **`intake/`** — `extract.py`, `receipts.py` (vision receipt extraction), `reconcile.py`, `report.py`.
- **`eval/`** — `runner.py`, `judge.py` (validated LLM-as-judge with an `agreement()` gate).
- **`web/`** — multi-tenant FastAPI: `app.py`, `db.py`, `pipeline.py`, `worker.py`,
  `customer_view.py`, `email.py`, `telegram_approval.py`, `turnstile.py` (CAPTCHA), `models.py`.
- **CLI:** `cli.py` exposes `ifta run | review | ask | chat | telegram-bot | rates`.

## 3. Key design decisions
1. **Math is deterministic; the LLM only reviews.** The trust boundary that makes the output
   safe for a government filing. The agent is grounded by 18 tools so it cites real numbers
   rather than inventing them.
2. **Rates are data, not code.** A new quarter = a new cached rate matrix; calculation logic
   is unchanged.
3. **Layered eval harness.** Heuristics+prompt (input) → benchmark (**gate**, CLI exits non-zero)
   → tracing (observability) → span/trajectory eval (regression guardrail) → rubric + validated
   judge (advisory). A model never grades its own filing call.
4. **Cost by risk tier.** Haiku/Sonnet for routine, Opus for high-risk, `--effort` for depth.
5. **Cheap, real deployment.** Mac mini + Cloudflare Tunnel (no public IP, no server bill) +
   Vercel frontend.

## 4. Data model (core)
`MileageRow{ jurisdiction, miles }` · `FuelRow{ jurisdiction, gallons, date, tax_paid }` ·
`Quarter{ year, q }` · `JurisdictionRate{ jurisdiction, rate, surcharge }` ·
`ReturnLine{ jurisdiction, taxable_gallons, tax, credit, net }`.

## 5. Testing strategy
- **Unit** — calculation per jurisdiction, surcharge handling, edge cases.
- **Integration** — full ingest→return pipeline; multi-tenant web flows.
- **Regression** — assert fleet MPG/miles/total tax match real historical filings to the penny.
- **Eval** — benchmark gate on extractor/agent accuracy vs. human-checked gold.
- **427 tests** across 38 files.

## 6. Risks & mitigations
- *Rate-table staleness* → packet marked `REVIEW REQUIRED` if the quarter's rates are unavailable.
- *LLM hallucination* → deterministic math + grounded tools + benchmark gate.
- *PII leakage* → receipts/labels/traces/IDs git-ignored; tokens + CAPTCHA + rate limiting.
