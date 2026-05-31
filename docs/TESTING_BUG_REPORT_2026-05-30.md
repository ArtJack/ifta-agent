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

## Recommended Fixes

### BUG-001 remediation: reserve unique upload paths

Recommended implementation:

- Update `_save_upload()` to choose a non-existing destination before writing.
- Preserve the sanitized filename for the first upload.
- Add a deterministic suffix before the extension for collisions, for example
  `file_same.csv`, `file_same_2.csv`, and `file_same_3.csv`.
- Keep the existing size-limit rollback behavior.
- Apply the same rule to `/submit` and `/submit/add/{token}` because both call
  `_save_upload()`.

Suggested regression tests:

- POST `/submit` with two `files[]` parts named `same.csv`; assert both files
  exist and retain their original contents.
- POST `/submit/add/{token}` with a filename that already exists in the
  submission inbox; assert the supplement is retained under a unique name.
- Repeat with sanitized names that collide, such as `a b.csv` and `a_b.csv`.

### BUG-002 remediation: restore distinct summary filenames

Recommended implementation:

- Change `CUSTOMER_SUMMARY_FILENAME` to `summary_report.md`.
- Leave `CUSTOMER_SUMMARY_PDF_FILENAME` as `summary_report.pdf`.
- Keep customer email attachment selection PDF-only.
- Keep the markdown artifact operator-only, as the existing comments and email
  attachment filtering already describe.

Suggested regression tests:

- Run `process_submission()` and assert that both `summary_report.md` and
  `summary_report.pdf` exist.
- Assert the markdown file decodes as UTF-8 text.
- Assert the PDF starts with `%PDF`.
- Assert `EmailClient.send_packet()` attaches the PDF but not the markdown
  debug artifact.

### BUG-003 remediation: reject ambiguous explicit client selection

Recommended implementation:

- Treat an explicit `--client` value as a strict selector.
- If the normalized client is not registered and the nested
  `inbox/<client_id>/<quarter>` path does not exist, raise a user-facing error
  instead of falling back to `inbox/<quarter>`.
- Preserve the shared legacy layout only when `--client` is omitted.
- Consider extracting the path choice into one helper that returns both inbox
  and output paths so the two decisions cannot diverge.

Suggested regression tests:

- Run path resolution with `client=None`; assert the legacy shared inbox still
  works.
- Run path resolution with a registered client alias; assert the intended
  nested inbox is used when present.
- Run path resolution with `client="does_not_exist"`; assert a concise error
  and verify that the shared quarter inbox is not processed.

### BUG-004 remediation: validate fleet-size business bounds before persistence

Recommended implementation:

- Add a bounded FastAPI form type, for example a Pydantic
  `Annotated[int, Field(ge=1, le=10000)]`, or perform an equivalent explicit
  validation check before `db.create_submission()`.
- Choose the upper bound based on supported business volume; `10000` is a
  conservative placeholder, not a regulatory limit.
- Return a clear `422` or `400` response for out-of-range values.
- Ensure values outside SQLite's signed 64-bit range never reach persistence.

Suggested regression tests:

- Test `fleet_size=0`, `1`, upper-bound, and upper-bound-plus-one.
- Test a negative value.
- Test `2^63` and a non-numeric value; both must return validation responses,
  never `500`.

### BUG-005 remediation: validate base state against a canonical set

Recommended implementation:

- Reuse the project's canonical jurisdiction constants if they already model
  the accepted base jurisdictions.
- Otherwise add one shared set for valid US state or supported IFTA base codes.
- Normalize whitespace and case first, then reject codes outside the set.
- Decide explicitly whether Canadian provinces are supported for onboarding;
  encode that choice in the shared set and tests.

Suggested regression tests:

- Accept normalized valid values such as `ca` and ` KY `.
- Reject `ZZ`, one-character values, three-character values, and unsupported
  jurisdictions.
- Cover any intentionally supported Canadian province codes.

### BUG-006 remediation: normalize CLI quarter parsing

Recommended implementation:

- Parse the `rates` quarter argument through the same `quarter_key()` or
  `_parse_quarter()` path used by the other CLI commands.
- Convert `ValueError` into `click.ClickException`.
- Keep the accepted format guidance consistent across `run`, `rates`, and
  other quarter-aware commands.

Suggested regression tests:

- Invoke `ifta rates --quarter 2026-Q9`; assert a non-zero exit code, concise
  message, and no traceback.
- Invoke with `Q4-2025` and `4Q2025`; assert both normalize successfully.

### BUG-007 remediation: constrain terminal DB updates

Recommended implementation:

- Add expected-source-state predicates to the SQL updates.
- Restrict `mark_done()` to `WHERE id = ? AND status = 'running'`.
- Define the legitimate failure sources and restrict `mark_failed()` to those
  states. At minimum, terminal `done` and `rejected` rows should not be
  overwritten.
- Return whether an update occurred, or return the current row, so callers can
  detect rejected transitions.
- Keep stale-running recovery as its own explicit transition.

Suggested regression tests:

- Assert `running -> done` succeeds.
- Assert `pending_approval -> done` is rejected.
- Assert an active processing state may become `failed`.
- Assert `done -> failed` and `rejected -> failed` are rejected.
- Assert stale-running recovery still marks only expired `running` rows as
  failed.

### Static-quality remediation

Recommended implementation:

- Type the Telegram edit payload using a JSON-compatible mapping accepted by
  `requests.post()`.
- Narrow `query.message` before reading Telegram message identifiers, and use
  the supported accessor for `MaybeInaccessibleMessage`.
- Run Ruff autofix on the notebook and review the resulting cell changes.

Suggested verification:

```bash
.venv/bin/mypy src
.venv/bin/ruff check .
.venv/bin/pytest
```

## Recommended Fix Order

1. Fix duplicate `files[]` filename handling.
2. Fix the summary markdown/PDF path collision.
3. Reject unknown CLI clients instead of falling back silently.
4. Add validated bounds for `fleet_size` and validate base-state membership.
5. Guard terminal DB transitions.
6. Convert `ifta rates` parsing errors to concise Click errors.
7. Resolve strict typing and notebook lint issues.
