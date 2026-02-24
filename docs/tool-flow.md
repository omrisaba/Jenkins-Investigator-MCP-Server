# Tool Flow

## Decision Tree — Which Tool to Use

```
User asks about a Jenkins job
         │
         ▼
   ┌─────────────┐
   │ Know the job │──No──► list_jobs(folder)
   │    name?     │        or triage_folder(folder)
   └──────┬──────┘
          │ Yes
          ▼
   ┌──────────────────┐
   │ What's the goal? │
   └──────┬───────────┘
          │
          ├── "Why did it fail?" ──────► investigate_build_failure ─┐
          │                                                         │
          ├── "What changed?" ─────────► compare_failing_vs_passing │
          │                                                         │
          ├── "Is it flaky?" ──────────► analyze_flaky_job          │
          │                                                         │
          ├── "Is it the node?" ───────► diagnose_infrastructure    │
          │                             _issue                      │
          ├── "Which test broke?" ─────► deep_dive_test_failures    │
          │                                                         │
          ├── "Where else does this                                 │
          │    error appear?" ─────────► search_across_jobs         │
          │                                                         │
          └── "Team health?" ──────────► triage_folder              │
                                                                    │
                                    Need more detail? ◄─────────────┘
                                         │
                     ┌───────────────────┬┴──────────────────┐
                     ▼                   ▼                   ▼
              get_stage_logs      search_console_log   get_build_artifacts
              (zoom into a        (grep for a           (fetch artifact
               failed stage)       specific pattern)     content)
```

## Bundle Internals

### investigate_build_failure

The primary entry point for root-cause analysis. Replaces 7+ sequential tool calls
with a single invocation.

```
investigate_build_failure(job_name, selector)
         │
         ▼
  get_named_build ──── fail? ──► short-circuit, return error
         │
         │ build_number, actions, changeSet
         │
         ├──► get_pipeline_stages ─── stages table with ◄◄◄ markers
         │
         ├──► get_console_text ──► log_parser.get_error_log(max_lines=150)
         │
         ├──► get_test_report ──── top 10 failing tests
         │
         ├──► extract_changesets ── from build data (no extra API call)
         │
         ├──► extract_parameters ── from build data (no extra API call)
         │
         └──► get_build_history ── last 10 builds, trend line
                                   (P P P F F F = "last 3 failed")

Output sections: BUILD INFO | PIPELINE STAGES | ERROR SUMMARY |
                 TEST FAILURES | SCM CHANGES | PARAMETERS | RECENT TREND
Hard cap: 400 lines
API calls: 4-5 (vs 7+ with individual tools)
```

### compare_failing_vs_passing

Finds the last passing build and diffs everything that changed.

```
compare_failing_vs_passing(job_name, failing_build)
         │
         ├──► get_named_build (if failing_build=0)
         │
         ├──► get_build_history(25) ── find last SUCCESS
         │         │
         │         └── No pass found? ──► "long-standing breakage"
         │
         ├──► get_build(failing) + get_build(passing)
         │         │
         │         ├── Parameter diff (added/removed/changed)
         │         ├── Agent diff
         │         └── Trigger diff
         │
         └──► get_build(each gap build) ── cumulative commits
              (max 10, sampling first 3 + last 3 if gap > 10)

Output sections: COMPARISON | PARAMETER DIFF | INFRA DIFF | CUMULATIVE COMMITS
Hard cap: 200 lines
```

### deep_dive_test_failures

Tracks when each test first broke and which commit likely caused it.
When JUnit XML artifacts are available, enriches with failure classification,
blast-radius detection, and extended stdout/stderr.

```
deep_dive_test_failures(job_name, build_number)
         │
         │  Pass 1: Regression detection
         ├──► get_test_report(build_number) ── top 10 failing tests
         │
         ├──► get_build_history(6) ── previous 5 builds
         │
         ├──► get_test_report(each previous build) ── was test passing?
         │
         ├──► get_build(regression build) ── suspect commits
         │         for each test:
         │           History: #95:PASS → #96:PASS → #97:FAIL → #98:FAIL
         │           Status: NEW failure (or persistent)
         │           Suspect: [abc123] dev: "refactored auth module"
         │
         │  Pass 2: JUnit XML enrichment (best-effort)
         ├──► discover_junit_artifacts ── find TEST-*.xml etc.
         │
         ├──► get_artifact_content_raw(each, max 8 files)
         │
         └──► junit_parser.parse_junit_xml ── for each file:
                    │
                    ├── Provenance: which files parsed, tests/failures per file
                    │
                    ├── Classification: N assertion failures, M exceptions
                    │
                    ├── Blast radius: suite with 3+ failures sharing same error
                    │     → collapsed to single root cause (saves tokens)
                    │
                    └── Extended detail: stdout/stderr, timing, Caused-by chains
                          (only for non-blast failures)

Output sections: TEST FAILURE ANALYSIS | (per test) | XML TEST DETAIL
Hard cap: 250 lines (XML enrichment budget: 80 lines)
API calls: 8-21 (regression: 8-13, XML: 1-8 best-effort)
```

### analyze_flaky_job

Statistical analysis of intermittent failures.

```
analyze_flaky_job(job_name, window=25)
         │
         ├──► get_build_history(25) ── result sequence
         │         │
         │         ├── Flakiness score (transitions / total)
         │         ├── Cluster by node (failure rate per agent)
         │         └── Cluster by time (business hours vs off-hours)
         │
         └──► get_pipeline_stages(each failing build, max 10)
                    └── Cluster by stage

Output: FLAKINESS ANALYSIS | BY NODE | BY TIME | BY STAGE | VERDICT
Hard cap: 150 lines
```

### diagnose_infrastructure_issue

Determines if a failure is node-related using exactly 3 API calls.

```
diagnose_infrastructure_issue(job_name, build_number)
         │
         ├──► get_build ──── which agent?
         │
         ├──► get_node_info(agent)
         │         │
         │         ├── Online/offline status
         │         ├── Disk space (< 10 GB = warning)
         │         └── 404? → ephemeral agent, note it
         │
         └──► get_build_history(20) ── per-node failure correlation
                    │
                    └── "node-01 fails 80% vs fleet avg 30%" → infra-related

Output: INFRASTRUCTURE DIAGNOSIS | NODE HEALTH | FAILURE CORRELATION | VERDICT
Hard cap: 100 lines
```

### triage_folder

Scans a folder for broken jobs — a team health dashboard.

```
triage_folder(folder, recursive=false)
         │
         ├──► get_folder_jobs(folder)
         │         │
         │         ├── Separate: real jobs vs subfolders
         │         ├── Classify: healthy / failing / unstable / other
         │         └── Count summary
         │
         ├──► (if recursive) get_folder_jobs(each subfolder, max 10)
         │
         └──► get_build_history(each failing job, top 10)
                    └── Consecutive failure count

Output: FOLDER TRIAGE | FAILING JOBS (with streak) | UNSTABLE JOBS
Hard cap: 200 lines
```

### search_across_jobs

Searches console logs across all jobs in a folder for a specific error pattern.

```
search_across_jobs(folder, pattern, ...)
         │
         ├──► get_folder_jobs(folder, include_last_failed=true)
         │         │
         │         ├── Filter by status (all / failing / unstable_and_failing)
         │         ├── Prioritise: failing > unstable > healthy
         │         └── Resolve build number per job (last or last_failed)
         │
         ├──► (if recursive) get_folder_jobs(each subfolder, max 10)
         │
         └──► ThreadPoolExecutor(8 workers):
                    for each job (max 50):
                      get_console_text_tail(~500 KB)
                      regex search with context lines
                      early termination at 30 total matches

Output: SEARCH RESULTS grouped by job, match count, snippets
Hard cap: 300 lines
API calls: 1 (discovery) + up to 50 (log fetches, concurrent)
```

## Individual Tools — Quick Reference

### Discovery & Navigation
| Tool | API Calls | Output Cap | Use Case |
|------|-----------|------------|----------|
| `get_last_build_info` | 1 | ~10 lines | Find build number from job name |
| `list_jobs` | 1 | ~100 lines | Browse Jenkins folder contents |

### Build Context
| Tool | API Calls | Output Cap | Use Case |
|------|-----------|------------|----------|
| `get_build_summary` | 1 | ~6 lines | Status, runtime, trigger |
| `get_pipeline_stages` | 1 | ~30 lines | Stage table with durations |
| `get_build_parameters` | 1 | ~20 lines | Build params (branch, env) |
| `get_build_history` | 1 | ~30 lines | Recent results + trend |
| `get_scm_changes` | 1 | ~20 lines | Commits in this build |

### Deep Investigation
| Tool | API Calls | Output Cap | Use Case |
|------|-----------|------------|----------|
| `get_error_logs` | 1 | 350 lines | Prioritized error extract from full console |
| `get_stage_logs` | 2 | 300 lines | Error extract from one stage's log |
| `search_console_log` | 1 | 200 lines | Grep for a pattern with context |
| `get_test_failures` | 1 | ~50 lines | Failing tests with stack traces |

### Artifacts & Config
| Tool | API Calls | Output Cap | Use Case |
|------|-----------|------------|----------|
| `get_build_artifacts` | 1 | ~100 lines | List or fetch artifact content |
| `get_job_config` | 1 | ~40 lines | Parsed config (SCM, triggers, agent) |
| `get_build_environment` | 1 | ~60 lines | Env vars (filtered, requires EnvInject) |
| `get_queue_info` | 1 | ~50 lines | Why builds are waiting |

### Infrastructure & Flow
| Tool | API Calls | Output Cap | Use Case |
|------|-----------|------------|----------|
| `get_node_status` | 1 | ~5 lines | Agent health + disk |
| `get_node_list` | 1 | ~50 lines | All agents with labels |
| `get_pipeline_flow_nodes` | 2 | ~30 lines | Parallel branches in a stage |
| `compare_builds` | 1 | ~30 lines | Diff two builds (params, agent, commits) |
| `get_upstream_downstream_builds` | 1-5 | ~20 lines | Build trigger chain |

## Stage Name Resolution

Several tools accept a `stage_name` parameter. Resolution uses three tiers:

```
Input: "test"
         │
         ├── 1. Exact match ──── "test" == "test"? ──► Use it
         │
         ├── 2. Case-insensitive ── "test" == "Test"? ──► Use it (if unique)
         │
         └── 3. Substring ──── "test" in "Unit Tests"? ──► Use it (if unique)
                                  │
                                  ├── 1 match ──► Use it
                                  ├── 2+ matches ──► "Ambiguous: Unit Tests, Integration Tests"
                                  └── 0 matches ──► "Not found. Available: Build, Unit Tests, ..."
```
