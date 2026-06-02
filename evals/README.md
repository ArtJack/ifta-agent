# IFTA Agent Evals

Regression tests for the agent. Run **before and after** any change to:
- `src/ifta/agent/prompts.py` (system prompt)
- `src/ifta/agent/tools.py` (tool defs/returns)
- `data/clients/*/profile.json` (narratives or thresholds)
- `data/regulations.json`

## Usage

```bash
# Run all cases (~$0.50, 1-2 min)
ifta eval

# Run one specific case
ifta eval --case q4_2025_menshikov_baseline

# Show full agent response for every case (not just failures)
ifta eval --verbose
```

Exit code is `0` if every assertion passes, `1` if any fails.

## Case format

One JSON file per case under `cases/`. Schema:

```json
{
  "name": "short_kebab_id",
  "description": "What this case guards against in one line.",
  "command": "review" | "ask",
  "quarter": "Q4-2025",
  "client": "menshikov_llc",
  "question": "...",               // ask only
  "model": "claude-opus-4-7",      // optional
  "effort": "low",                  // optional
  "max_tokens": 2048,               // optional
  "assertions": {
    "total_tax_due": 795.16,        // review only — exact value
    "must_mention": [...],          // case-insensitive substrings
    "must_not_mention": [...],
    "min_summary_len": 100,
    "min_issues": 1,
    "structural": {                 // review only
      "has_summary": true,
      "has_issues": true,
      "has_filing_reminders": true,
      "has_next_steps": true
    },
    "tools": {                      // trajectory — graded against the agent's trace
      "must_call": ["query_return"],
      "must_not_call": ["read_past_filing"],
      "must_call_in_order": ["get_review_packet", "lookup_rate"],
      "max_calls": 12
    }
  }
}
```

The `tools` block is a **span / trajectory** assertion: it grades *which tools the
agent actually called* (captured via the agent trace), not just its text output.
`ifta eval` prints each case's `trajectory:` line so you can see the real sequence.

## What's in the starter set

| Case | Guards against |
|---|---|
| `01_q4_2025_menshikov_baseline` | Compute regression — must hit $795.16 + name client correctly |
| `02_q1_2026_david_baseline` | Multi-truck math — DM EXPRESS Q1 2026 = $3,216.33 |
| `03_q2_2026_test_logistics_baseline` | Unknown-client path — no leak from real clients |
| `04_isolation_david_with_menshikov_question` | Multi-tenant identity — refuse to apply wrong client's thresholds |
| `05_injection_ignore_instructions` | Prompt injection — keep performing audit review under pressure |

## Cost

Each case is one agent invocation. With `effort: low` and `claude-opus-4-7` the 5-case suite costs **~$0.50** per run. Cheap enough to run weekly or before every commit that touches agent code.

## Adding a case

1. Drop a new `NN_<id>.json` in `cases/`.
2. Run `ifta eval --case <id>` to see the agent's actual response **and its `trajectory:` line** (the tools it called).
3. Set assertions narrowly — each assertion is one regression guardrail, not a complete behavioral spec. For a `tools` block, assert the trajectory you *observed* (the `trajectory:` line, or `ifta review … --trace`) — don't guess it.
4. If a customer ever says "the agent missed X this quarter," add an assertion that catches it. That's how the suite earns its keep over time.
