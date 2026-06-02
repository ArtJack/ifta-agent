# Agent Tracing

The review agent is a multi-step tool-using loop (`tool_runner` over 16 tools). Tracing
makes that loop visible: every model turn, the tools it called (with inputs), per-turn
tokens, and the final answer + filing status. It's the input to the next eval layer —
span/trajectory evaluation over the agent's steps.

## Use

```bash
ifta review --quarter Q1-2026 --client dm_express --trace
```

Prints the trace and saves it to `data/traces/<quarter>-<ts>.json`. Without `--trace`
nothing changes — tracing is **opt-in via a context manager**, so `run_agent` records only
when a trace is active (zero overhead, identical behavior otherwise).

## What a trace captures

- **Turns** — each model call: text, whether it used extended thinking, the tool calls it
  requested (name + input), and input/output tokens.
- **Tool sequence** — the ordered tool names the agent called (`trace.tool_sequence()`) —
  the basis for **span / trajectory evaluation**: *did it call the right tools, in a
  sensible order?*
- **Outcome** — final text, the deterministic `filing_status`, wall time, estimated cost.

## Why

You can't evaluate or debug *how* an agent decides until you can see its steps. A passing
final answer can still hide a bad trajectory (skipped a tool, looked up the wrong rate).
Traces turn the loop from a black box into something you can inspect and score.

## Data

Tool inputs reference a client/quarter, so traces are customer-adjacent — `data/traces/`
is **git-ignored**. The tracing code and tests are PII-free.

## Next

- ✅ **Span / trajectory evaluator** — a case's `tools` block in `ifta eval` grades the
  agent's tool sequence (must_call / must_not_call / order / budget) against the trace.
- Capture tool *outputs* (wrap the tool functions) for full step-level inspection.
- A rubric + validated LLM judge over the trace for qualitative review quality.
