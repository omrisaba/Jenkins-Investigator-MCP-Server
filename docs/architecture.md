# Architecture

## System Overview

The Jenkins Investigator MCP Server sits between an AI assistant and a Jenkins instance.
Its job is to transform Jenkins' verbose API responses into concise, token-efficient
output that fits inside an AI's context window.

```
┌─────────────────────┐
│   AI Assistant       │
│  (Cursor / Claude)   │
└────────┬────────────┘
         │  MCP Protocol
         │  (Streamable HTTP or stdio)
         ▼
┌─────────────────────┐
│   FastMCP Server     │  server.py
│                     │
│  ┌───────────────┐  │
│  │ Individual    │  │  20 tools — each wraps a single API call
│  │ Tools         │  │  with output pruning and formatting
│  └───────┬───────┘  │
│          │          │
│  ┌───────────────┐  │
│  │ Bundle Tools  │  │  7 bundles — composite tools that call
│  │               │  │  API wrappers directly (not other tools)
│  └───────┬───────┘  │
│          │          │
│  ┌───────────────┐  │
│  │ XML Enrichment│  │  _enrich_with_junit_xml() — best-effort
│  │               │  │  JUnit XML parsing for deep_dive bundle
│  └───────┬───────┘  │
│          │          │
│  ┌───────────────┐  │
│  │ Error Handler │  │  _handle_error() — consistent error
│  │               │  │  messages across all tools
│  └───────────────┘  │
└────────┬────────────┘
         │
         ▼
┌─────────────────────┐
│   Utility Layer      │  utils/
│                     │
│  jenkins_api.py     │  HTTP wrappers + response pruning
│  junit_parser.py    │  JUnit XML parsing + classification
│  log_parser.py      │  Priority-budgeted error extraction
│  scm.py             │  Changeset normalization (Git/SVN/Hg)
└────────┬────────────┘
         │
         │  REST API (Basic Auth)
         │  with retry/backoff
         ▼
┌─────────────────────┐
│   Jenkins Instance   │
│                     │
│  /api/json          │  Build data, test reports, queue
│  /wfapi/            │  Pipeline stages, flow nodes
│  /consoleText       │  Full console logs
│  /config.xml        │  Job configuration
│  /artifact/         │  Build artifacts
└─────────────────────┘
```

## Layer Responsibilities

### server.py — Tool Definitions

All 27 MCP tools are defined here. The file has two kinds:

**Individual tools** call a single `jenkins_api` function, format the result,
and apply `TOOL_DELAY` once before returning.

**Bundle tools** call multiple `jenkins_api` functions directly (never other MCP tools)
and apply `_BUNDLE_PACING` (50ms) between sub-calls to avoid hammering Jenkins.
They apply `TOOL_DELAY` only once at the end. This avoids the N × delay penalty
that would occur if bundles called other tools.

The `deep_dive_test_failures` bundle also includes a best-effort XML enrichment
pass via `_enrich_with_junit_xml()`, which discovers and parses JUnit XML build
artifacts to provide failure classification (assertion vs exception), blast-radius
detection (many tests sharing one root cause), and extended stdout/stderr context.
This enrichment is entirely optional — it activates only when JUnit XML artifacts
are archived, and degrades silently when they are not.

### utils/jenkins_api.py — API Wrappers

Every Jenkins REST call goes through `_get()`, which provides:

- **Basic authentication** from environment variables
- **SSL verification** (configurable)
- **Retry with backoff** for transient failures (HTTP 429, 502, 503, 504)
  and connection/timeout errors — up to 2 retries with 1s and 3s delays
- **URL encoding** of job path segments via `_job_path()` to handle
  spaces, `#`, `%`, and other special characters

Response pruning happens at this layer using Jenkins' `tree` query parameter
(server-side filtering) and Python-side field selection.

### utils/log_parser.py — Error Extraction

The `get_error_log()` function implements a priority-budgeted extraction strategy:

```
Full log (could be 100K+ lines)
        │
        ▼
┌─────────────────┐
│ Severity scan    │  Classify every line: CRITICAL > ERROR > WARNING
└────────┬────────┘
         ▼
┌─────────────────┐
│ Stage resolution │  Map each match to its pipeline stage
└────────┬────────┘
         ▼
┌─────────────────┐
│ Deduplication    │  Fingerprint = tier + exception token + stage + message
│                 │  Keep first occurrence, count repeats
└────────┬────────┘
         ▼
┌─────────────────┐
│ Budget fill      │  CRITICAL sections first, then ERROR, then WARNING
│                 │  Oversized sections are clipped, never dropped
└────────┬────────┘
         ▼
┌─────────────────┐
│ Anchor lines     │  First 5 lines (context) + last 30 lines (result)
└────────┬────────┘
         ▼
    ≤ 250 lines output (hard cap: 350)
```

The function is parameterizable: bundles pass custom `max_lines`, `hard_limit`,
`include_head`, and `include_tail` values to fit their per-section budgets.

### utils/junit_parser.py — JUnit XML Parsing

A pure parser with no API calls or formatting logic. Accepts raw XML content and
returns structured Python dicts. Three public functions:

- `parse_junit_xml()` — Parses standard JUnit XML (`<testsuite>` or `<testsuites>`
  root). Extracts per-case status, timing, message, detail, stdout, stderr.
  Returns `None` for non-JUnit XML.

- `classify_failures()` — Counts assertion failures vs exception errors across
  all parsed suites. Uses a 3-tier heuristic: the `type` attribute on `<failure>`
  or `<error>` elements is checked first (e.g. `junit.framework.AssertionError`
  → assertion, `java.io.IOException` → exception), then the message content is
  scanned for keywords, and the element tag (`<failure>` vs `<error>`) is used
  only as a fallback. This handles frameworks like pytest that put all failures
  under `<failure>` regardless of cause.

- `detect_blast_radius()` — Finds suites where 3+ failures share the same root
  error (≥60% threshold). When detected, the consuming code collapses N individual
  test entries into a single blast-radius summary, saving tokens and immediately
  surfacing the shared root cause.

### utils/scm.py — SCM Normalization

Normalizes the different changeset formats Jenkins uses (`changeSet` vs `changeSets`,
Git vs SVN vs Mercurial) into a uniform list of `{commit_id, author, message}` dicts.

## Token Efficiency Strategy

Every layer contributes to keeping output small:

| Layer | Technique |
|-------|-----------|
| Jenkins API | `tree` parameter prunes response server-side |
| `jenkins_api.py` | `_BUILD_KEEP_FIELDS` drops unneeded keys |
| `log_parser.py` | Line budgets, dedup, severity prioritization |
| `junit_parser.py` | Parse XML → extract only failing tests; blast-radius collapses N failures into ~4 lines |
| `server.py` tools | Output hard caps (150–400 lines per tool) |
| `server.py` bundles | Per-section budgets within overall hard cap |
| `server.py` XML enrichment | 80-line budget, provenance tracking, only additive info from XML |
| MCP instructions | Guide AI to bundles first, individual tools only for follow-up |

## Retry and Resilience

```
Request
   │
   ├── Success (2xx) ──────────────► Return response
   │
   ├── Retryable (429/502/503/504)
   │     ├── Attempt 1: wait 1s ──► Retry
   │     └── Attempt 2: wait 3s ──► Retry or raise
   │
   ├── Connection/Timeout error
   │     ├── Attempt 1: wait 1s ──► Retry
   │     └── Attempt 2: wait 3s ──► Retry or raise
   │
   └── Non-retryable (401/403/404/etc.) ──► Raise immediately
```

## Graceful Degradation

The server handles missing capabilities without crashing:

| Scenario | Behavior |
|----------|----------|
| Freestyle job (no pipeline) | `get_pipeline_stages` returns `None`, tools report "not a Pipeline job" |
| No EnvInject plugin | `get_injected_env_vars` returns `None`, tool says "plugin not installed" |
| Ephemeral/cloud agent gone | `get_node_info` catches 404, infra bundle still shows correlation data |
| No test report | `get_test_report` returns `None`, bundle skips test section |
| No JUnit XML artifacts | XML enrichment returns `None`, section silently omitted |
| JUnit XML not JUnit format | Root element check rejects non-JUnit XML, file marked `[SKIP]` in provenance |
| Binary artifact | `get_artifact_content` returns `None`, tool says "binary file" |
| No SCM changes | Tools report "no changes recorded" |
