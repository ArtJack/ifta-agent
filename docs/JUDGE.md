# Rubric + validated LLM judge

The last layer of the eval stack. The heuristics, benchmark, tracing, and
trajectory evals all check things you can grade *mechanically* — a field is right
or wrong, a tool was called or it wasn't. But "is this review note any **good**?"
is a judgment call. This layer makes that judgment cheap and repeatable with an
LLM-as-judge — and, critically, gives you the tool to decide whether to trust it.

`src/ifta/eval/judge.py` · `ifta review --judge` · `tests/test_judge.py`

## What it judges (and what it does NOT)

The judge scores the **quality of the review note as a document**, on a 4-point
rubric, each criterion `0` / `1` / `2`:

| Criterion | Question |
|---|---|
| `coverage` | Does it address the deterministic filing blockers and the return's material issues? |
| `grounding` | Are the numbers specific and internally consistent — nothing invented or contradictory? |
| `clarity` | Is it clear and actionable for a non-expert owner-operator? |
| `filing_alignment` | Does the tone match the filing status (never implies "ready to file" when it's `DO_NOT_FILE`)? |

`overall` is the normalized mean (sum ÷ `2·N`), reported as a percentage.

**The judge does not decide whether to file.** That decision is deterministic —
`_enforce_deterministic_filing_status` in `agent/runner.py` is authoritative, and
the judge is *handed* that status so it can grade alignment rather than re-litigate
it. A model grading its own filing decision is a circular gate; we don't build one.
The judge is a **quality signal at scale**, not a control.

## The discipline: never trust an unvalidated judge

An LLM judge is only worth as much as its **agreement with a human** on the
criteria you care about. Before you rely on any judge score, validate it:

1. Score a sample of real notes yourself (you are the oracle — same role you
   played for the receipt eval).
2. Run the judge over the same notes.
3. Compare with `agreement(judge_scores, human_scores)`:

```python
from ifta.eval.judge import agreement

agreement(
    {"coverage": 2, "grounding": 1, "clarity": 2, "filing_alignment": 2},
    {"coverage": 2, "grounding": 2, "clarity": 2, "filing_alignment": 2},  # your gold
)
# -> per_criterion deltas, exact_rate, within1_rate
```

`agreement` reports **per criterion**, because a judge is rarely uniformly good.
It may nail `coverage` and `filing_alignment` (concrete, checkable) while drifting
on `clarity` (subjective). Trust the criteria where `exact_rate` / `within1_rate`
hold up; treat the rest as advisory only. A judge that disagrees with you is not a
judge you get to cite.

This is the same loop the receipt eval taught: the harness is only as trustworthy
as its gold labels, and the gold labels are only as trustworthy as the human who
checked them. We caught two "dangerous" receipt errors that were actually *oracle*
mistakes — proof the validation step is not ceremony.

## Usage

```bash
# Attach the judge to a real review (advisory — printed after the note):
ifta review --quarter Q4-2025 --client menshikov_llc --judge

# Combine with --trace to see the path AND the quality in one run:
ifta review --quarter Q4-2025 --client menshikov_llc --trace --judge
```

The judge runs as a **separate** model call on the finished note, so it never
perturbs the agent's own run or metrics. If the judge call fails, the review still
succeeds — the judge is advisory and prints `judge unavailable: …` rather than
taking the review down with it.

## Why it's not a gate

The benchmark (`docs/BENCHMARK.md`) gates the *pipeline* on mechanical, validated
metrics — tax-safe rate, field accuracy, regression deltas — because those are
grounded in human-checked gold and fail loudly. An LLM judge's score is a softer,
model-derived signal; gating a tax filing on it would let one model's opinion block
or wave through a return. So the judge **informs**, the deterministic status
**decides**, and the benchmark **gates**. Keep those three jobs separate.

## Where this sits in the eval stack

```
heuristics ──► benchmark (gated) ──► tracing ──► trajectory/span eval ──► rubric + validated judge
  prompt          regression          visibility    tool-path grading        note-quality grading
  knowledge       guardrail                                                   (advisory, validated)
```

Mechanical evals catch *wrong*. The judge catches *weak* — notes that are correct
but unhelpful. Both matter; only the mechanical ones are allowed to gate.
