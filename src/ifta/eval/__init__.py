"""IFTA agent evaluation framework.

See `evals/cases/*.json` for case definitions, `runner.py` for the engine,
and `ifta eval` for the CLI entry point.
"""

from ifta.eval.judge import (
    RUBRIC,
    JudgeResult,
    agreement,
    judge_review,
    render_judge,
)
from ifta.eval.runner import (
    AssertionResult,
    CaseResult,
    EvalCase,
    grade_assertions,
    grade_trajectory,
    load_cases,
    run_case,
)

__all__ = [
    "RUBRIC",
    "AssertionResult",
    "CaseResult",
    "EvalCase",
    "JudgeResult",
    "agreement",
    "grade_assertions",
    "grade_trajectory",
    "judge_review",
    "load_cases",
    "render_judge",
    "run_case",
]
