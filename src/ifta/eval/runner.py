"""Agent evaluation framework.

Loads JSON case files from `evals/cases/`, runs each case through the agent
(`review` or `ask`), grades the response against the case's assertions, and
prints a pass/fail report with cost + diff for failures.

A case is a JSON document of this shape:

    {
      "name": "q4_2025_menshikov_baseline",
      "description": "...",
      "command": "review" | "ask",
      "quarter": "Q4-2025",
      "client": "menshikov_llc",
      "question": "...",            // ask only
      "model": "claude-sonnet-4-6", // optional
      "effort": "low",              // optional
      "max_tokens": 2048,           // optional
      "assertions": {
        "total_tax_due": 795.16,    // review only — exact match on summary's total
        "must_mention": ["KY surcharge", "Oregon"],
        "must_not_mention": ["DM EXPRESS", "David"],
        "min_summary_len": 100,
        "min_issues": 1,
        "structural": {             // review only
          "has_summary": true,
          "has_issues": true,
          "has_filing_reminders": true,
          "has_next_steps": true
        },
        "tools": {                  // trajectory — graded against the agent's trace
          "must_call": ["query_return"],
          "must_not_call": ["read_past_filing"],
          "must_call_in_order": ["get_review_packet", "lookup_rate"],
          "max_calls": 12
        }
      }
    }

Keep cases small and assertions focused — each assertion is one regression
guardrail, not a complete behavioral spec. Write `tools` assertions *after*
observing a real run (`ifta review --trace`, or the `trajectory:` line in
`ifta eval`) so you assert the path the agent actually takes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ifta.agent.metrics import AgentMetrics
from ifta.agent.runner import ReviewNote, ask, review

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CASES_DIR = PROJECT_ROOT / "evals" / "cases"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_EFFORT = "low"
DEFAULT_MAX_TOKENS = 2048


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    """One eval scenario, loaded from JSON."""

    name: str
    description: str
    command: str
    quarter: str
    client: str | None = None
    question: str | None = None
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS
    assertions: dict[str, Any] = field(default_factory=dict)
    source_path: Path | None = None

    @classmethod
    def from_json(cls, path: Path) -> EvalCase:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name=payload["name"],
            description=payload.get("description", ""),
            command=payload["command"],
            quarter=payload["quarter"],
            client=payload.get("client"),
            question=payload.get("question"),
            model=payload.get("model", DEFAULT_MODEL),
            effort=payload.get("effort", DEFAULT_EFFORT),
            max_tokens=payload.get("max_tokens", DEFAULT_MAX_TOKENS),
            assertions=payload.get("assertions", {}),
            source_path=path,
        )


def load_cases(cases_dir: Path | None = None) -> list[EvalCase]:
    cases_dir = cases_dir or DEFAULT_CASES_DIR
    if not cases_dir.exists():
        return []
    return [
        EvalCase.from_json(p)
        for p in sorted(cases_dir.glob("*.json"))
        if not p.name.startswith("_")
    ]


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


@dataclass
class AssertionResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class CaseResult:
    case: EvalCase
    response_text: str
    note: ReviewNote | None
    metrics: AgentMetrics | None
    assertions: list[AssertionResult]
    error: str | None = None
    tool_sequence: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.error is None and all(a.passed for a in self.assertions)

    @property
    def num_passed(self) -> int:
        return sum(1 for a in self.assertions if a.passed)

    @property
    def num_failed(self) -> int:
        return sum(1 for a in self.assertions if not a.passed)


def grade_assertions(
    assertions: dict[str, Any],
    *,
    response_text: str,
    note: ReviewNote | None,
) -> list[AssertionResult]:
    """Apply each assertion in `assertions` to the agent response.

    `note` is the parsed ReviewNote when the command was `review`; None for
    `ask`. Some assertion keys are review-only and are skipped silently for
    `ask` responses.
    """
    results: list[AssertionResult] = []
    text_lower = response_text.lower()

    must_mention = assertions.get("must_mention") or []
    for substr in must_mention:
        ok = substr.lower() in text_lower
        results.append(
            AssertionResult(
                name=f"must_mention[{substr!r}]",
                passed=ok,
                detail="" if ok else "missing substring",
            )
        )

    must_not_mention = assertions.get("must_not_mention") or []
    for substr in must_not_mention:
        ok = substr.lower() not in text_lower
        results.append(
            AssertionResult(
                name=f"must_not_mention[{substr!r}]",
                passed=ok,
                detail="" if ok else "forbidden substring leaked into response",
            )
        )

    if note is not None:
        summary_text = note.summary or ""
        if "total_tax_due" in assertions:
            expected = assertions["total_tax_due"]
            # Search the summary for the numeric value (allowing commas + $).
            tokens = [
                t.strip(",$.")
                for t in summary_text.replace(",", "").split()
            ]
            ok = any(t == f"{expected:.2f}" or t == str(expected) for t in tokens)
            if not ok:
                # Also search whole response (review notes sometimes put it in issues).
                ok = (
                    f"{expected:.2f}" in response_text
                    or str(expected) in response_text
                )
            results.append(
                AssertionResult(
                    name=f"total_tax_due={expected}",
                    passed=ok,
                    detail=""
                    if ok
                    else f"expected total_tax_due={expected} not found in response",
                )
            )

        min_summary_len = assertions.get("min_summary_len")
        if min_summary_len is not None:
            ok = len(summary_text) >= min_summary_len
            results.append(
                AssertionResult(
                    name=f"min_summary_len>={min_summary_len}",
                    passed=ok,
                    detail="" if ok else f"summary was {len(summary_text)} chars",
                )
            )

        min_issues = assertions.get("min_issues")
        if min_issues is not None:
            ok = len(note.issues) >= min_issues
            results.append(
                AssertionResult(
                    name=f"min_issues>={min_issues}",
                    passed=ok,
                    detail="" if ok else f"only {len(note.issues)} issue(s)",
                )
            )

        structural = assertions.get("structural") or {}
        section_check = {
            "has_summary": bool(note.summary.strip()),
            "has_issues": bool(note.issues),
            "has_filing_reminders": bool(note.filing_reminders),
            "has_next_steps": bool(note.next_steps),
        }
        for key, expected in structural.items():
            got = section_check.get(key)
            if got is None:
                continue
            ok = got == expected
            results.append(
                AssertionResult(
                    name=f"structural.{key}={expected}",
                    passed=ok,
                    detail="" if ok else f"got {got}",
                )
            )

    return results


# ---------------------------------------------------------------------------
# Trajectory (span) grading
# ---------------------------------------------------------------------------


def _is_subsequence(needles: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(n in it for n in needles)


def grade_trajectory(tools_spec: dict[str, Any], tool_sequence: list[str]) -> list[AssertionResult]:
    """Grade the agent's tool-call trajectory against a case's `tools` assertions."""
    results: list[AssertionResult] = []
    called = set(tool_sequence)
    for tool in tools_spec.get("must_call") or []:
        ok = tool in called
        results.append(AssertionResult(f"must_call[{tool}]", ok, "" if ok else "tool was not called"))
    for tool in tools_spec.get("must_not_call") or []:
        ok = tool not in called
        results.append(
            AssertionResult(f"must_not_call[{tool}]", ok, "" if ok else "forbidden tool was called")
        )
    order = tools_spec.get("must_call_in_order")
    if order:
        ok = _is_subsequence(list(order), tool_sequence)
        results.append(
            AssertionResult(f"must_call_in_order{list(order)}", ok, "" if ok else f"actual: {tool_sequence}")
        )
    max_calls = tools_spec.get("max_calls")
    if max_calls is not None:
        ok = len(tool_sequence) <= max_calls
        results.append(
            AssertionResult(f"max_calls<={max_calls}", ok, "" if ok else f"made {len(tool_sequence)} calls")
        )
    min_calls = tools_spec.get("min_calls")
    if min_calls is not None:
        ok = len(tool_sequence) >= min_calls
        results.append(
            AssertionResult(f"min_calls>={min_calls}", ok, "" if ok else f"made only {len(tool_sequence)} calls")
        )
    return results


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_case(case: EvalCase) -> CaseResult:
    """Execute one eval case end-to-end. Catches exceptions so a broken case
    doesn't stop the rest of the suite. The agent run is traced so the case's
    `tools` assertions can grade the tool-call trajectory."""
    from ifta.agent.tracing import traced

    started = time.monotonic()
    try:
        with traced(case.name, case.model) as trace:
            if case.command == "review":
                note, metrics = review(
                    case.quarter,
                    client=case.client,
                    model=case.model,
                    max_tokens=case.max_tokens,
                    effort=case.effort,
                )
                response_text = _serialize_review(note)
            elif case.command == "ask":
                if not case.question:
                    raise ValueError(f"case {case.name!r}: command=ask requires a 'question' field")
                response_text = ask(
                    case.question,
                    quarter=case.quarter,
                    client=case.client,
                    model=case.model,
                    max_tokens=case.max_tokens,
                    effort=case.effort,
                )
                note = None
                metrics = None  # ask() doesn't return metrics today
            else:
                raise ValueError(f"case {case.name!r}: unknown command {case.command!r}")
    except Exception as e:
        return CaseResult(
            case=case,
            response_text="",
            note=None,
            metrics=None,
            assertions=[],
            error=f"{type(e).__name__}: {e}",
        )

    tool_sequence = trace.tool_sequence()
    assertions = grade_assertions(case.assertions, response_text=response_text, note=note)
    tools_spec = case.assertions.get("tools")
    if tools_spec:
        assertions += grade_trajectory(tools_spec, tool_sequence)
    if metrics is not None:
        metrics.wall_time_seconds = round(time.monotonic() - started, 2)
    return CaseResult(
        case=case,
        response_text=response_text,
        note=note,
        metrics=metrics,
        assertions=assertions,
        tool_sequence=tool_sequence,
    )


def _serialize_review(note: ReviewNote) -> str:
    """Flatten a ReviewNote to text so substring assertions hit every field."""
    parts: list[str] = [note.filing_status or "", note.summary or ""]
    parts.extend(note.filing_status_reasons)
    for section in (note.issues, note.filing_reminders, note.next_steps):
        for item in section:
            if isinstance(item, dict):
                parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
    return "\n".join(parts)
