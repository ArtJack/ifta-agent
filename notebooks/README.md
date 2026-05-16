# `notebooks/` — Prompt + tool experimentation scratchpad

This is where you try things **before** committing them to `src/ifta/agent/`.

| Notebook | Purpose |
|---|---|
| `01_prompt_workbench.ipynb` | Iterate on system prompts, tool descriptions, and quick agent calls without restarting Python every time. |

## Setup

The Jupyter kernel `IFTA Pipeline (.venv)` is already installed
(`python -m ipykernel install --user --name=ifta-pipeline ...`). It points
at this project's venv so all imports work the same as in `src/`.

When you open a notebook in VSCode, pick the **IFTA Pipeline (.venv)** kernel
in the top-right.

## Convention

- Notebooks are **scratchpads** — output gets committed but notebooks
  themselves are *not* production code.
- When a prompt or tool stabilizes, port it into the relevant module
  under `src/ifta/agent/` and write a test if appropriate.
- Number notebooks (`01_`, `02_`, ...) so they appear in order.
