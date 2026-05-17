"""IFTA agent evaluation framework.

See `evals/cases/*.json` for case definitions, `runner.py` for the engine,
and `ifta eval` for the CLI entry point.
"""

from ifta.eval.runner import (
    AssertionResult,
    CaseResult,
    EvalCase,
    grade_assertions,
    load_cases,
    run_case,
)

__all__ = [
    "AssertionResult",
    "CaseResult",
    "EvalCase",
    "grade_assertions",
    "load_cases",
    "run_case",
]
