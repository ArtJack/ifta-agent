"""LLM-as-judge for review-note quality, with a validation (agreement) mechanism.

The deterministic filing gate decides *whether to file*. This judge scores the
*quality of the review note as a document* — does it cover the deterministic
blockers, stay grounded, read clearly, and align with the filing status — on a
small rubric, via a separate structured model call.

Crucially, an LLM judge is only trustworthy where it AGREES with a human. The
`agreement` helper compares judge scores to human gold scores so you can validate
the judge *per criterion* before relying on it. Never gate a filing on a judge —
that stays with the deterministic gate; the judge is a quality signal at scale.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_SCORE = 2


@dataclass
class Criterion:
    name: str
    question: str


RUBRIC: tuple[Criterion, ...] = (
    Criterion(
        "coverage",
        "Does it address the deterministic filing reasons (if any) and the return's "
        "material issues? 0=misses them, 1=partial, 2=addresses all.",
    ),
    Criterion(
        "grounding",
        "Are claims and numbers specific and internally consistent, with no invented "
        "figures or contradictions? 0=hallucinated/contradictory, 1=minor, 2=fully grounded.",
    ),
    Criterion(
        "clarity",
        "Clear and actionable for a non-expert owner-operator? 0=confusing, 1=ok, 2=crisp.",
    ),
    Criterion(
        "filing_alignment",
        "Does the tone match the filing status (never implies 'ready to file' when the "
        "status is DO_NOT_FILE, or vice versa)? 0=contradicts, 1=ambiguous, 2=aligned.",
    ),
)

JUDGE_PROMPT_TEMPLATE = """You evaluate the QUALITY of a pre-filing review note an AI wrote for a trucking IFTA fuel-tax return.

You are NOT re-deciding whether to file — the filing decision is made deterministically and is given to you below. Judge ONLY the review note as a document, against the rubric.

Filing status (deterministic, authoritative): {filing_status}
Filing-status reasons: {filing_reasons}

Rubric — score each 0, 1, or 2:
{rubric}

Review note to judge:
---
{review}
---

Return ONLY a JSON object: an integer 0-2 for every rubric key, plus a "rationale" object mapping each key to a one-sentence reason. Example:
{{"coverage": 2, "grounding": 2, "clarity": 1, "filing_alignment": 2, "rationale": {{"coverage": "...", "grounding": "...", "clarity": "...", "filing_alignment": "..."}}}}
Return the JSON now."""

JudgeCall = Callable[[str], dict[str, Any]]


@dataclass
class JudgeResult:
    scores: dict[str, int]
    rationale: dict[str, str]

    @property
    def overall(self) -> float:
        if not self.scores:
            return 0.0
        return sum(self.scores.values()) / (MAX_SCORE * len(self.scores))


def _rubric_text() -> str:
    return "\n".join(f"- {c.name}: {c.question}" for c in RUBRIC)


def build_prompt(
    review_text: str, *, filing_status: str | None = None, filing_reasons: list[str] | None = None
) -> str:
    return JUDGE_PROMPT_TEMPLATE.format(
        filing_status=filing_status or "(unknown)",
        filing_reasons="; ".join(filing_reasons or []) or "(none)",
        rubric=_rubric_text(),
        review=review_text,
    )


def parse_judge(payload: dict[str, Any]) -> JudgeResult:
    scores: dict[str, int] = {}
    for c in RUBRIC:
        value = payload.get(c.name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        scores[c.name] = max(0, min(MAX_SCORE, int(round(value))))
    raw = payload.get("rationale")
    rationale = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    return JudgeResult(scores=scores, rationale=rationale)


def _judge_live(prompt: str, *, model: str, max_tokens: int) -> dict[str, Any]:
    from ifta.agent.runner import _client, _extract_review_json

    client = _client()
    message = client.messages.create(
        model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}]
    )
    text = "".join(b.text for b in message.content if b.type == "text")
    return _extract_review_json(text)


def judge_review(
    review_text: str,
    *,
    filing_status: str | None = None,
    filing_reasons: list[str] | None = None,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
    call: JudgeCall | None = None,
) -> JudgeResult:
    """Score a review note on the rubric. `call` overrides the live judge (for tests)."""
    prompt = build_prompt(review_text, filing_status=filing_status, filing_reasons=filing_reasons)
    runner = call or (lambda p: _judge_live(p, model=model, max_tokens=max_tokens))
    return parse_judge(runner(prompt))


def agreement(judge: dict[str, int], human: dict[str, int]) -> dict[str, Any]:
    """Per-criterion agreement between judge and human gold scores.

    Run over a labeled set to decide which criteria the judge is trustworthy on
    (exact-match and within-1 rates) before relying on it.
    """
    per: dict[str, Any] = {}
    exact = within1 = total = 0
    for c in RUBRIC:
        if c.name not in judge or c.name not in human:
            continue
        delta = abs(judge[c.name] - human[c.name])
        per[c.name] = {"judge": judge[c.name], "human": human[c.name], "delta": delta}
        total += 1
        exact += delta == 0
        within1 += delta <= 1
    return {
        "per_criterion": per,
        "exact_rate": exact / total if total else None,
        "within1_rate": within1 / total if total else None,
    }


def render_judge(result: JudgeResult) -> str:
    lines = ["Review-quality judge (0-2 each):"]
    for c in RUBRIC:
        score = result.scores.get(c.name)
        shown = "—" if score is None else f"{score}/2"
        lines.append(f"  {c.name:16} {shown}  {result.rationale.get(c.name, '')}".rstrip())
    lines.append(f"  overall: {result.overall:.0%}")
    return "\n".join(lines)


__all__ = [
    "RUBRIC",
    "Criterion",
    "JudgeResult",
    "agreement",
    "build_prompt",
    "judge_review",
    "parse_judge",
    "render_judge",
]
