# IFTA Benchmark

How the agent's two model surfaces are benchmarked — turned from ad-hoc evals into
**gated, tracked** suites you run before any prompt or model change.

## Two surfaces, two benchmarks

| Surface | Output | Benchmark | Gate |
|---|---|---|---|
| **Receipt extraction** | structured fields (ground-truthable) | `ifta benchmark` over the labeled gold set | thresholds + regression, **exits non-zero on fail** |
| **Review agent** | open-ended ReviewNote | `ifta eval` over `evals/cases/*.json` | assertions (`must_mention`, `total_tax_due`, structural) |

Both are **deliberately run** (they hit the live model — cost + non-determinism), so
they live *outside* the free offline `pytest` suite. Run them before shipping a change
that touches a prompt, a tool, or the model.

## Receipt-extraction benchmark

```bash
# 1. produce predictions for the current extractor (costs $; one vision call per receipt)
ifta receipt-eval run --model claude-sonnet-4-6

# 2. gate them: score vs gold + thresholds, compare to the previous run, record the scorecard
ifta benchmark --model claude-sonnet-4-6
#    -> PASS / FAIL (non-zero exit on FAIL), regressions vs last run, appended to history
```

### The gate (`DEFAULT_THRESHOLDS`)
Tax-critical fields (`date`, `state`, `gallons`) are held hardest — a confidently wrong
one can mis-file a return:

| check | threshold |
|---|---|
| tax-safe rate (all 3 tax fields correct) | ≥ 95% |
| dangerous tax errors (wrong/hallucinated) | **0** |
| date / state / gallons accuracy | ≥ 95% each |
| any field's drop vs the previous run | ≤ 3 points |

A failed threshold **or** a regression beyond tolerance exits non-zero, so this can sit
in front of a merge. Non-tax fields (`card_last4`, `vendor`, …) are tracked but not gated
— a dip there shouldn't block a tax-safe change.

### Tracking over time & across models
Each run appends a compact, **PII-free** scorecard (model, date, tax-safe rate, dangerous
count, per-field accuracy — no receipt content) to `evals/benchmark_history.jsonl`. To
**compare models**, run each and read the history:

```bash
ifta receipt-eval run --model claude-opus-4-7 --out evals/predictions/opus.json
ifta benchmark --model claude-opus-4-7 --predictions evals/predictions/opus.json
```

This is also how you vet a **model upgrade**: re-run the benchmark on the new model and
confirm tax-critical holds before switching.

## Why a gate, not just a report
The receipt eval already showed why: a well-intentioned prompt change (the operator
heuristics) regressed `card_last4` 91%→62% and nudged a tax field. A *report* shows that;
a *gate* stops it from shipping. See [`RECEIPT_EVAL_RESULTS.md`](RECEIPT_EVAL_RESULTS.md).

## Data handling
Gold labels, predictions, and `benchmark_history.jsonl` are customer-derived and
**git-ignored**. The runner, thresholds, tests, and this doc are PII-free.

## Next
Unify the review-agent surface (`ifta eval`) under the same gate/history model, and add
per-field span evaluation once the review agent is traced.
