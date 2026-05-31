# IFTA Agent Testing Bug Report

Date: 2026-05-30

## Scope

This report records defects reproduced during local and isolated testing of the
IFTA Agent project. Production submissions were not modified. Mutation-heavy
tests used temporary SQLite databases and temporary submission directories.

Testing completed:

- Unit suite: `349 passed`
- Integration regression suite: `180 passed`
- Static checks: Ruff, compileall, import sweep, dependency consistency, and
  tracked-secret checks
- Dynamic, exploratory, black-box, white-box, functional, non-functional, and
  bounded performance testing
- Test-design techniques: equivalence partitioning, boundary-value analysis,
  decision-table testing, and state-transition testing

Not covered end to end:

- Real paid AI review calls
- Real customer email delivery
- Real Telegram button interaction
- Rendered browser interaction, because the in-app browser connection was
  unavailable

## Confirmed Defects

### BUG-001: Duplicate multi-file upload names overwrite earlier files

Severity: High

Location: `src/ifta/web/app.py`, `_save_upload()`

Description:

The `files[]` upload field prefixes every uploaded filename with `file_`.
When two files have the same original name, both resolve to the same destination
path and the later upload silently overwrites the earlier upload.

Reproduction:

1. Create an isolated FastAPI app instance with temporary DB and submission
   paths.
2. POST `/submit` with two `files[]` parts named `same.csv`.
3. Use different content in each part: `first` and `second`.
4. Inspect the saved inbox directory.

Expected:

Both uploads are retained under unique names, for example:

```text
file_same.csv
file_same_2.csv
```

Actual:

Only one file remains:

```text
file_same.csv -> second
```

Impact:

A customer upload can be silently reduced to partial data, producing an
incomplete or incorrect filing packet.

### BUG-002: Markdown customer summary is overwritten by PDF bytes

Severity: Medium

Locations:

- `src/ifta/web/customer_view.py`
- `src/ifta/web/pipeline.py`

Description:

Both summary filename constants are set to `summary_report.pdf`:

```python
CUSTOMER_SUMMARY_FILENAME = "summary_report.pdf"
CUSTOMER_SUMMARY_PDF_FILENAME = "summary_report.pdf"
```

The pipeline writes the markdown summary first, then immediately overwrites the
same path with PDF bytes.

Expected:

The output directory contains both:

```text
summary_report.md
summary_report.pdf
```

Actual:

Only `summary_report.pdf` remains.

Impact:

Customer PDF delivery still works, but the documented operator/debug markdown
artifact is missing.

### BUG-003: Unknown CLI client silently falls back to a shared quarter inbox

Severity: Medium

Location: `src/ifta/client.py`, `resolve_inbox()` and `resolve_output_dir()`

Reproduction:

```bash
.venv/bin/ifta run --quarter Q4-2025 --client does_not_exist
```

Expected:

The command rejects the unknown client or reports that the requested client
inbox does not exist.

Actual:

The command falls back to `inbox/Q4-2025`, reads that directory's
`client.json`, and processes the return as `menshikov_llc`.

Impact:

A typo in a client identifier can process the wrong carrier's data.

### BUG-004: Fleet-size metadata lacks bounds validation

Severity: Medium

Location: `src/ifta/web/app.py`, `/submit`

Boundary-value results:

| Input | Actual response |
| --- | --- |
| `fleet_size=-1` | `202 Accepted` |
| `fleet_size=0` | `202 Accepted` |
| `fleet_size=1` | `202 Accepted` |
| `fleet_size=9223372036854775807` | `202 Accepted` |
| `fleet_size=9223372036854775808` | `500 Internal Server Error` |

Expected:

Invalid business values and values outside the SQLite integer range return a
clear `400` or `422` validation response.

Impact:

Invalid metadata is persisted, and sufficiently large inputs produce an
uncaught server error.

### BUG-005: Base-state validation accepts unknown two-letter codes

Severity: Low

Location: `src/ifta/web/app.py`, `/submit`

Reproduction:

POST `/submit` with:

```text
base_state=ZZ
```

Expected:

Reject values outside the supported US state or jurisdiction set.

Actual:

The API returns `202 Accepted` and persists `ZZ`.

Impact:

Invalid carrier metadata can reach downstream review and operator workflows.

### BUG-006: Invalid `ifta rates` quarter prints a Python traceback

Severity: Low

Location: `src/ifta/cli.py`, `rates()`

Reproduction:

```bash
.venv/bin/ifta rates --quarter 2026-Q9
```

Expected:

A concise Click error explaining the accepted quarter formats.

Actual:

The CLI prints a Python traceback ending in:

```text
ValueError: unrecognised quarter: 2026-Q9
```

Impact:

Operator-facing CLI behavior is noisy and inconsistent with `ifta run`.

### BUG-007: Terminal DB helpers permit invalid direct state transitions

Severity: Low

Location: `src/ifta/web/db.py`, `mark_done()` and `mark_failed()`

Description:

The terminal update helpers do not constrain the current source state.

Reproduced invalid transitions:

```text
pending_approval -> done
done -> failed
```

Expected:

`mark_done()` updates only `running` submissions. `mark_failed()` updates only
states that may legitimately fail.

Impact:

Current worker usage follows the intended sequence, but future callers or
operator tooling can corrupt lifecycle state.

## Static Quality Issues

### STATIC-001: Strict type checking reports two errors

Severity: Low

Command:

```bash
.venv/bin/mypy src
```

Errors:

```text
src/ifta/web/telegram_approval.py:213:
Argument "json" to "post" has incompatible type "dict[str, object]";
expected "JsonType"

src/ifta/telegram_bot.py:2794:
"MaybeInaccessibleMessage" has no attribute "chat_id"
```

### STATIC-002: Full-repository Ruff check reports notebook-only lint errors

Severity: Low

Command:

```bash
.venv/bin/ruff check .
```

Result:

Six fixable import-order and unused-import errors in
`notebooks/01_prompt_workbench.ipynb`.

Application lint remains clean:

```bash
.venv/bin/ruff check src tests scripts
```

## Passing Risk Controls

The following behaviors passed targeted testing:

- Upload size boundary: `1 MB` accepted, `1 MB + 1 byte` rejected with rollback
- Submit rate limit: first two accepted, third rejected with friendly `429`
- CORS allow and deny paths
- CAPTCHA enforcement and valid backend-key bypass
- Path-traversal filename sanitization
- Approval, request-more-files, supplement, rejection, and confirmation flows
- Closed add-files windows return `409`
- Stale running jobs are reaped without affecting recent running jobs
- SQLite integrity check returns `ok`; WAL mode is enabled
- Public and local health endpoints remain available under bounded read-only load

## Performance Snapshot

Bounded read-only and isolated deterministic performance tests:

| Surface | Result |
| --- | --- |
| Local `/healthz`, 40 workers | `3787 req/s`, `14.33 ms` max |
| Public `/healthz`, 20 workers | `91.4 req/s`, `514.75 ms` max |
| Public `/healthz`, 40 workers | One `5.42 s` outlier; useful range is below this level |
| Deterministic pipeline warm median | `10.30 ms` per fixture packet |
| Deterministic pipeline peak parallel throughput | `102.1 jobs/s` at 8 workers |

The deterministic pipeline benchmark excludes AI review and outbound email
latency.

## Recommended Fix Order

1. Fix duplicate `files[]` filename handling.
2. Fix the summary markdown/PDF path collision.
3. Reject unknown CLI clients instead of falling back silently.
4. Add validated bounds for `fleet_size` and validate base-state membership.
5. Guard terminal DB transitions.
6. Convert `ifta rates` parsing errors to concise Click errors.
7. Resolve strict typing and notebook lint issues.
