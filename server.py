"""
Jenkins Investigator MCP Server

A Model Context Protocol server that acts as a high-performance filter between
the verbose Jenkins API and the AI's limited context window.

Transport: Streamable HTTP by default (MCP_TRANSPORT=http, host 0.0.0.0, port 8000).
           Set MCP_TRANSPORT=stdio to use stdio instead (e.g. for Cursor/Claude Desktop).
Logs:      All application logs go to stderr to avoid corrupting the JSON-RPC stream.
"""

import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP

from utils import jenkins_api, log_parser, scm

load_dotenv()

# Route all library and application logs to stderr, never stdout.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("jenkins-mcp")

TOOL_DELAY = float(os.getenv("TOOL_DELAY_SECONDS", "2"))

mcp = FastMCP(
    "Jenkins Investigator",
    instructions=(
        "You are a Jenkins CI debugging assistant. "
        "Start with investigate_build_failure for a complete RCA picture of any failing job — "
        "it gives you build info, stages, errors, tests, commits, parameters, and trend in one call. "
        "Use compare_failing_vs_passing to understand what changed between the last pass and first fail. "
        "Use search_across_jobs to find a specific error across all jobs in a folder in one call. "
        "For follow-up, use individual tools: get_stage_logs to zoom into a stage, "
        "search_console_log to grep for a pattern, get_build_artifacts for artifacts. "
        "Use list_jobs to discover jobs, triage_folder for team health. "
        "Use analyze_flaky_job for intermittent failures, diagnose_infrastructure_issue for node problems. "
        "Use deep_dive_test_failures to find which commit broke which test. "
        "Only fall back to get_error_logs, get_build_summary, get_pipeline_stages, etc. "
        "when you need a single focused piece of data."
    ),
)


def _handle_error(exc: Exception, context: str) -> str:
    """Convert common exceptions into readable strings for the AI."""
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code
        if status == 401:
            return f"[{context}] Authentication failed (401). Check JENKINS_USER and JENKINS_TOKEN."
        if status == 404:
            return f"[{context}] Not found (404). Verify the job name and build number."
        return f"[{context}] Jenkins API error {status}: {exc.response.text[:300]}"
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return f"[{context}] {exc}"
    return f"[{context}] Unexpected error: {exc}"


# ---------------------------------------------------------------------------
# Discovery Tool
# ---------------------------------------------------------------------------


@mcp.tool
def get_last_build_info(job_name: str, selector: str = "last_failed") -> str:
    """Look up a named build (last_failed/last/last_stable/last_success)
    and return its number, status, duration. Use to discover build numbers.

    Args:
        job_name: Jenkins job name.
        selector: One of: last_failed, last, last_stable, last_success.
    """
    try:
        build = jenkins_api.get_named_build(job_name, selector)
    except Exception as exc:
        return _handle_error(exc, "get_last_build_info")
    finally:
        time.sleep(TOOL_DELAY)

    if not build:
        return f"No '{selector}' build found for job '{job_name}'. The job may have never run or never failed."

    triggered_by = jenkins_api.get_build_trigger(build.get("actions") or [])
    duration_s = round((build.get("duration") or 0) / 1000, 1)

    lines = [
        f"Job:        {job_name}",
        f"Selector:   {selector}",
        f"Build #:    {build.get('number', 'unknown')}",
        f"Result:     {build.get('result', 'IN_PROGRESS')}",
        f"Duration:   {duration_s}s",
        f"Agent:      {build.get('builtOn') or 'controller'}",
        f"Trigger:    {triggered_by}",
        f"",
        f"Use build number {build.get('number')} with get_error_logs, get_build_summary, etc.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Triage Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_build_summary(job_name: str, build_number: int) -> str:
    """High-level build overview: status, runtime, agent, trigger.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        build = jenkins_api.get_build(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_build_summary")
    finally:
        time.sleep(TOOL_DELAY)

    triggered_by = jenkins_api.get_build_trigger(build.get("actions") or [])
    duration_s = round((build.get("duration") or 0) / 1000, 1)

    ts = build.get("timestamp")
    started = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if ts else "unknown"
    )

    lines = [
        f"Job:        {job_name} #{build_number}",
        f"Result:     {build.get('result', 'IN_PROGRESS')}",
        f"Started:    {started}",
        f"Duration:   {duration_s}s",
        f"Agent:      {build.get('builtOn') or 'controller'}",
        f"Trigger:    {triggered_by}",
    ]
    return "\n".join(lines)


@mcp.tool
def get_scm_changes(job_name: str, build_number: int) -> str:
    """Show commits in this build (Git/SVN/Hg).

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        build = jenkins_api.get_build(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_scm_changes")
    finally:
        time.sleep(TOOL_DELAY)

    commits = scm.extract_changesets(build)
    if not commits:
        return "No SCM changes recorded for this build (may be a manual trigger or a pipeline with no checkout step)."

    lines = [f"Commits in {job_name} #{build_number} ({len(commits)} total):"]
    for c in commits:
        lines.append(f"\n  [{c['commit_id']}] {c['author']}")
        lines.append(f"  {c['message']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Context Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_pipeline_stages(job_name: str, build_number: int) -> str:
    """Show status and duration of each pipeline stage.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        stages = jenkins_api.get_pipeline_stages(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_pipeline_stages")
    finally:
        time.sleep(TOOL_DELAY)

    if stages is None:
        return "No stage data available. This job is not a Pipeline (Declarative/Scripted) build."

    if not stages:
        return "Pipeline has no stages recorded for this build."

    lines = [f"Pipeline stages for {job_name} #{build_number}:\n"]
    lines.append(f"  {'Stage':<30} {'Status':<15} {'Duration':>10}")
    lines.append(f"  {'-'*30} {'-'*15} {'-'*10}")
    for s in stages:
        lines.append(f"  {s['name']:<30} {s['status']:<15} {s['duration_s']:>9.1f}s")

    failed = [s for s in stages if s["status"] not in ("SUCCESS", "NOT_EXECUTED")]
    if failed:
        names = ", ".join(s["name"] for s in failed)
        lines.append(f"\nFailed/unstable stages: {names}")
        lines.append("Use get_error_logs to inspect the console output for details.")

    return "\n".join(lines)


@mcp.tool
def get_build_parameters(job_name: str, build_number: int) -> str:
    """Show build parameters (branch, environment, flags).

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        build = jenkins_api.get_build(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_build_parameters")
    finally:
        time.sleep(TOOL_DELAY)

    params = jenkins_api.extract_parameters(build.get("actions") or [])

    if not params:
        return f"Build {job_name} #{build_number} has no parameters (not a parameterized job, or was triggered with defaults only)."

    lines = [f"Parameters for {job_name} #{build_number}:\n"]
    for p in params:
        lines.append(f"  {p['name']} = {p['value']}")
    return "\n".join(lines)


@mcp.tool
def get_build_history(job_name: str, count: int = 10) -> str:
    """Show recent build results with trend summary.

    Args:
        job_name: Jenkins job name.
        count: Number of recent builds (default 10, max 25).
    """
    try:
        builds = jenkins_api.get_build_history(job_name, count)
    except Exception as exc:
        return _handle_error(exc, "get_build_history")
    finally:
        time.sleep(TOOL_DELAY)

    if not builds:
        return f"No build history found for job '{job_name}'. The job may have never been built."

    lines = [f"Recent builds for {job_name} (last {len(builds)}):\n"]
    lines.append(f"  {'#':<8} {'Result':<15} {'Duration':>10}  {'Started'}")
    lines.append(f"  {'-'*8} {'-'*15} {'-'*10}  {'-'*20}")
    for b in builds:
        ts = b.get("timestamp")
        started = (
            datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if ts else "unknown"
        )
        lines.append(
            f"  {b['number']:<8} {b['result']:<15} {b['duration_s']:>9.1f}s  {started}"
        )

    # Trend summary
    results = [b["result"] for b in builds]
    consecutive_fail = 0
    for r in results:
        if r in ("FAILURE", "ABORTED", "UNSTABLE"):
            consecutive_fail += 1
        else:
            break

    if consecutive_fail == 0:
        lines.append(f"\nTrend: Latest build passed. Last {len(builds)} builds look healthy.")
    elif consecutive_fail == len(builds):
        lines.append(f"\nTrend: All {len(builds)} recent builds failed — long-standing breakage.")
    else:
        lines.append(
            f"\nTrend: Last {consecutive_fail} build(s) failed; "
            f"the preceding build (#{builds[consecutive_fail]['number']}) passed."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deep-Dive Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_error_logs(job_name: str, build_number: int) -> str:
    """Extract errors from full console log (CRITICAL > ERROR > WARNING).
    Falls back to last 250 lines if no patterns match.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        console_text = jenkins_api.get_console_text(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_error_logs")
    finally:
        time.sleep(TOOL_DELAY)

    return log_parser.get_error_log(console_text)


@mcp.tool
def get_test_failures(job_name: str, build_number: int) -> str:
    """Show failing tests with error messages and stack traces.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        report = jenkins_api.get_test_report(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_test_failures")
    finally:
        time.sleep(TOOL_DELAY)

    if report is None:
        return "No test report found for this build. The job may not publish JUnit results."

    lines = [
        f"Test summary: {report['fail_count']} failed, "
        f"{report['pass_count']} passed, {report['skip_count']} skipped."
    ]

    if not report["failing_tests"]:
        lines.append("No individual test failures recorded (may be a compilation/setup failure).")
        return "\n".join(lines)

    lines.append(f"\nFailing tests ({len(report['failing_tests'])}):")
    for test in report["failing_tests"]:
        lines.append(f"\n  FAIL: {test['class_name']}.{test['test_name']}")
        if test["error_details"]:
            lines.append(f"  Error: {test['error_details']}")
        if test["error_stack_trace"]:
            lines.append(f"  Stack:\n{test['error_stack_trace']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stage & Search Tools
# ---------------------------------------------------------------------------


def _resolve_stage(stages: list[dict], stage_name: str) -> dict | str:
    """Find a stage by name using three-tier matching.

    Returns the matched stage dict, or a user-friendly error string listing
    available stages.
    """
    names = [s["name"] for s in stages]

    for s in stages:
        if s["name"] == stage_name:
            return s

    lower = stage_name.lower()
    ci_matches = [s for s in stages if s["name"].lower() == lower]
    if len(ci_matches) == 1:
        return ci_matches[0]

    sub_matches = [s for s in stages if lower in s["name"].lower()]
    if len(sub_matches) == 1:
        return sub_matches[0]

    if sub_matches:
        matched_names = ", ".join(f'"{s["name"]}"' for s in sub_matches)
        return (
            f"Ambiguous stage name '{stage_name}' — matches: {matched_names}. "
            "Please provide the exact stage name."
        )

    available = ", ".join(f'"{n}"' for n in names)
    return f"Stage '{stage_name}' not found. Available stages: {available}"


@mcp.tool
def get_stage_logs(job_name: str, build_number: int, stage_name: str) -> str:
    """Fetch error-focused log output for a specific pipeline stage.
    Use this after get_pipeline_stages identifies the failed stage.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
        stage_name: Name of the stage to inspect.
    """
    try:
        stages = jenkins_api.get_pipeline_stages(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_stage_logs")

    if stages is None:
        return "Not a Pipeline job — stage logs are only available for Pipeline builds."
    if not stages:
        return "Pipeline has no stages recorded for this build."

    match = _resolve_stage(stages, stage_name)
    if isinstance(match, str):
        time.sleep(TOOL_DELAY)
        return match

    try:
        raw_log = jenkins_api.get_stage_log(job_name, build_number, match["id"])
    except Exception as exc:
        return _handle_error(exc, "get_stage_logs")
    finally:
        time.sleep(TOOL_DELAY)

    if not raw_log.strip():
        return f"Stage '{match['name']}' has an empty log."

    header = f"Stage: {match['name']} | Status: {match['status']} | Duration: {match['duration_s']}s\n\n"
    return header + log_parser.get_error_log(
        raw_log, max_lines=250, hard_limit=300, include_head=False,
    )


_SEARCH_MAX_MATCHES = 20
_SEARCH_MAX_OUTPUT_LINES = 200


@mcp.tool
def search_console_log(
    job_name: str,
    build_number: int,
    pattern: str,
    context_lines: int = 3,
    is_regex: bool = False,
) -> str:
    """Search the console log for a specific string or regex pattern.
    Use this for targeted investigation when get_error_logs missed something.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
        pattern: Search term (literal by default).
        context_lines: Lines of context before/after each match (default 3).
        is_regex: If true, treat pattern as a regex.
    """
    try:
        console_text = jenkins_api.get_console_text(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "search_console_log")
    finally:
        time.sleep(TOOL_DELAY)

    if is_regex:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            compiled = re.compile(re.escape(pattern), re.IGNORECASE)
    else:
        compiled = re.compile(re.escape(pattern), re.IGNORECASE)

    lines = console_text.splitlines()
    match_indices = [i for i, line in enumerate(lines) if compiled.search(line)]

    if not match_indices:
        return f"No matches for '{pattern}' in {len(lines)} lines of console output."

    total_matches = len(match_indices)

    ranges: list[tuple[int, int]] = []
    for idx in match_indices[:_SEARCH_MAX_MATCHES]:
        start = max(0, idx - context_lines)
        end = min(len(lines) - 1, idx + context_lines)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    output_lines: list[str] = []
    output_lines.append(
        f"[Search: '{pattern}' — {total_matches} matches in {len(lines)} lines"
        + (f", showing first {_SEARCH_MAX_MATCHES}]" if total_matches > _SEARCH_MAX_MATCHES else "]")
    )
    output_lines.append("")

    line_count = 2
    for start, end in ranges:
        if line_count >= _SEARCH_MAX_OUTPUT_LINES:
            output_lines.append(f"[Output truncated at {_SEARCH_MAX_OUTPUT_LINES} lines]")
            break
        output_lines.append(f"--- lines {start + 1}-{end + 1} ---")
        for i in range(start, end + 1):
            marker = ">>>" if i in match_indices else "   "
            output_lines.append(f"{marker} {i + 1:>6}| {lines[i]}")
        output_lines.append("")
        line_count = len(output_lines)

    if total_matches > _SEARCH_MAX_MATCHES:
        output_lines.append(
            f"[{total_matches - _SEARCH_MAX_MATCHES} additional matches not shown — refine your pattern]"
        )

    return "\n".join(output_lines)


# ---------------------------------------------------------------------------
# Discovery & Listing Tools
# ---------------------------------------------------------------------------


_COLOR_MAP = {
    "blue": "SUCCESS", "blue_anime": "SUCCESS (building)",
    "red": "FAILURE", "red_anime": "FAILURE (building)",
    "yellow": "UNSTABLE", "yellow_anime": "UNSTABLE (building)",
    "grey": "DISABLED", "disabled": "DISABLED",
    "notbuilt": "NEVER BUILT", "aborted": "ABORTED",
    "aborted_anime": "ABORTED (building)",
}
_FOLDER_CLASSES = frozenset({
    "com.cloudbees.hudson.plugins.folder.Folder",
    "jenkins.branch.OrganizationFolder",
    "org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject",
})


@mcp.tool
def list_jobs(folder: str = "") -> str:
    """List jobs in a Jenkins folder (or root) with their current status.
    Use this to discover job names before investigating them.

    Args:
        folder: Folder path (empty = Jenkins root).
    """
    try:
        jobs = jenkins_api.get_folder_jobs(folder)
    except Exception as exc:
        return _handle_error(exc, "list_jobs")
    finally:
        time.sleep(TOOL_DELAY)

    if not jobs:
        return f"No jobs found in '{folder or '(root)'}'."

    real_jobs = [j for j in jobs if j.get("_class", "") not in _FOLDER_CLASSES]
    subfolders = [j for j in jobs if j.get("_class", "") in _FOLDER_CLASSES]

    lines = [f"Jobs in '{folder or '(root)'}' ({len(real_jobs)} jobs, {len(subfolders)} subfolders):\n"]
    lines.append(f"  {'Name':<40} {'Status':<25} {'Last Build'}")
    lines.append(f"  {'-'*40} {'-'*25} {'-'*15}")

    for j in real_jobs:
        status = _COLOR_MAP.get(j.get("color", ""), j.get("color", "?"))
        bn = j.get("last_build_number")
        last = f"#{bn}" if bn else "never"
        lines.append(f"  {j['name']:<40} {status:<25} {last}")

    if subfolders:
        lines.append(f"\nSubfolders ({len(subfolders)}):")
        for sf in subfolders:
            lines.append(f"  📁 {sf['name']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Artifact Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_build_artifacts(job_name: str, build_number: int, file_path: str = "") -> str:
    """List or fetch build artifacts.  Without file_path, lists all artifacts.
    With file_path, fetches the content of that specific artifact.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
        file_path: Relative path of artifact to fetch (empty = list all).
    """
    if not file_path:
        try:
            artifacts = jenkins_api.get_artifacts_list(job_name, build_number)
        except Exception as exc:
            return _handle_error(exc, "get_build_artifacts")
        finally:
            time.sleep(TOOL_DELAY)

        if not artifacts:
            return "No artifacts found for this build."

        lines = [f"Artifacts for {job_name} #{build_number} ({len(artifacts)} files):\n"]
        for a in artifacts[:100]:
            lines.append(f"  {a['relative_path']}")
        if len(artifacts) > 100:
            lines.append(f"  ... and {len(artifacts) - 100} more")
        lines.append("\nUse file_path parameter to fetch a specific artifact's content.")
        return "\n".join(lines)

    try:
        content = jenkins_api.get_artifact_content(job_name, build_number, file_path)
    except Exception as exc:
        return _handle_error(exc, "get_build_artifacts")
    finally:
        time.sleep(TOOL_DELAY)

    if content is None:
        return f"Cannot display '{file_path}' — binary file or undecodable content."

    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".log", ".txt", ".out"):
        return f"Artifact: {file_path}\n\n" + log_parser.get_error_log(
            content, max_lines=250, hard_limit=300,
        )

    return f"Artifact: {file_path}\n\n{content}"


# ---------------------------------------------------------------------------
# Comparison Tool
# ---------------------------------------------------------------------------


@mcp.tool
def compare_builds(job_name: str, build_a: int, build_b: int) -> str:
    """Compare two builds to find differences in parameters, agent, trigger, and commits.
    Only changed fields are shown.

    Args:
        job_name: Jenkins job name.
        build_a: First build number.
        build_b: Second build number.
    """
    try:
        data_a = jenkins_api.get_build(job_name, build_a)
        data_b = jenkins_api.get_build(job_name, build_b)
    except Exception as exc:
        return _handle_error(exc, "compare_builds")
    finally:
        time.sleep(TOOL_DELAY)

    sections: list[str] = []
    sections.append(
        f"Comparing {job_name} #{build_a} ({data_a.get('result', '?')}) "
        f"vs #{build_b} ({data_b.get('result', '?')})"
    )

    params_a = {p["name"]: p["value"] for p in jenkins_api.extract_parameters(data_a.get("actions") or [])}
    params_b = {p["name"]: p["value"] for p in jenkins_api.extract_parameters(data_b.get("actions") or [])}

    diff_lines = []
    for k in sorted(set(params_a) | set(params_b)):
        va, vb = params_a.get(k), params_b.get(k)
        if va != vb:
            if vb is None:
                diff_lines.append(f"  {k}: only in #{build_a} = {va}")
            elif va is None:
                diff_lines.append(f"  {k}: only in #{build_b} = {vb}")
            else:
                diff_lines.append(f"  {k}: #{build_a}={va}  |  #{build_b}={vb}")

    if diff_lines:
        sections.append("Parameter differences:\n" + "\n".join(diff_lines))
    else:
        sections.append("Parameters: identical")

    agent_a = data_a.get("builtOn") or "controller"
    agent_b = data_b.get("builtOn") or "controller"
    if agent_a != agent_b:
        sections.append(f"Agent: #{build_a}={agent_a}  |  #{build_b}={agent_b}")

    trigger_a = jenkins_api.get_build_trigger(data_a.get("actions") or [])
    trigger_b = jenkins_api.get_build_trigger(data_b.get("actions") or [])
    if trigger_a != trigger_b:
        sections.append(f"Trigger: #{build_a}={trigger_a}  |  #{build_b}={trigger_b}")

    commits_a = scm.extract_changesets(data_a)
    commits_b = scm.extract_changesets(data_b)
    if commits_a:
        cl = [f"  [{c['commit_id']}] {c['author']}: {c['message'][:100]}" for c in commits_a[:8]]
        sections.append(f"Commits in #{build_a} ({len(commits_a)}):\n" + "\n".join(cl))
    if commits_b:
        cl = [f"  [{c['commit_id']}] {c['author']}: {c['message'][:100]}" for c in commits_b[:8]]
        sections.append(f"Commits in #{build_b} ({len(commits_b)}):\n" + "\n".join(cl))

    dur_a = round((data_a.get("duration") or 0) / 1000, 1)
    dur_b = round((data_b.get("duration") or 0) / 1000, 1)
    if abs(dur_a - dur_b) > 10:
        sections.append(f"Duration: #{build_a}={dur_a}s  |  #{build_b}={dur_b}s (delta {dur_a - dur_b:+.1f}s)")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Causal Chain Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_upstream_downstream_builds(job_name: str, build_number: int) -> str:
    """Show the upstream trigger chain for a build.  Upstream is reliable;
    downstream is best-effort and depends on trigger plugins.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        build = jenkins_api.get_build(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_upstream_downstream_builds")
    finally:
        time.sleep(TOOL_DELAY)

    lines = [f"Build chain for {job_name} #{build_number}:\n"]

    # Walk upstream
    upstream_chain: list[str] = []
    actions = build.get("actions") or []
    for action in actions:
        for cause in action.get("causes") or []:
            if "upstreamProject" in cause:
                proj = cause["upstreamProject"]
                bn = cause.get("upstreamBuild", "?")
                upstream_chain.append(f"  ← {proj} #{bn}")

    depth = 0
    current_actions = actions
    while depth < 4:
        found = False
        for action in current_actions:
            for cause in action.get("causes") or []:
                if "upstreamProject" in cause:
                    try:
                        ub = jenkins_api.get_build(cause["upstreamProject"], int(cause.get("upstreamBuild", 0)))
                        result = ub.get("result", "?")
                        upstream_chain.append(f"    {'  ' * depth}← {cause['upstreamProject']} #{cause.get('upstreamBuild', '?')} ({result})")
                        current_actions = ub.get("actions") or []
                        found = True
                        depth += 1
                        break
                    except Exception:
                        break
            if found:
                break
        if not found:
            break

    if upstream_chain:
        lines.append("Upstream chain:")
        lines.extend(upstream_chain)
    else:
        lines.append("No upstream trigger detected (root build).")

    # Best-effort downstream
    downstream: list[str] = []
    for action in actions:
        cls = action.get("_class", "")
        if "BuildInfoExporter" in cls or "trigger" in cls.lower():
            for ref in action.get("triggeredBuilds") or []:
                downstream.append(f"  → {ref.get('fullProjectName', '?')} #{ref.get('number', '?')}")

    if downstream:
        lines.append("\nDownstream (best-effort, plugin-dependent):")
        lines.extend(downstream[:10])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Configuration & Environment Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_job_config(job_name: str) -> str:
    """Show the key configuration of a Jenkins job: type, SCM, triggers,
    agent, and pipeline definition.  Never returns raw XML.

    Args:
        job_name: Jenkins job name.
    """
    try:
        xml_str = jenkins_api.get_job_config_xml(job_name)
    except Exception as exc:
        return _handle_error(exc, "get_job_config")
    finally:
        time.sleep(TOOL_DELAY)

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return f"Could not parse config.xml for '{job_name}'."

    tag = root.tag
    job_type_map = {
        "flow-definition": "Pipeline",
        "project": "Freestyle",
        "maven2-moduleset": "Maven",
        "matrix-project": "Matrix",
    }
    job_type = job_type_map.get(tag)
    if not job_type:
        if "multibranch" in tag.lower() or "MultiBranch" in tag:
            job_type = "Multibranch Pipeline"
        elif "OrganizationFolder" in tag:
            job_type = "Organization Folder"
        else:
            job_type = tag.split(".")[-1] if "." in tag else tag

    lines = [f"Job: {job_name}", f"Type: {job_type}", ""]

    # SCM
    scm_el = root.find(".//scm")
    if scm_el is not None:
        scm_class = scm_el.get("class", "")
        url_el = scm_el.find(".//url") or scm_el.find(".//remote")
        branch_el = scm_el.find(".//name") or scm_el.find(".//branch")
        lines.append("SCM:")
        if "git" in scm_class.lower():
            lines.append(f"  Type: Git")
        elif "svn" in scm_class.lower():
            lines.append(f"  Type: SVN")
        else:
            lines.append(f"  Type: {scm_class.split('.')[-1]}")
        if url_el is not None and url_el.text:
            lines.append(f"  URL: {url_el.text.strip()}")
        if branch_el is not None and branch_el.text:
            lines.append(f"  Branch: {branch_el.text.strip()}")
        lines.append("")

    # Triggers
    triggers_el = root.find(".//triggers")
    if triggers_el is not None and len(triggers_el):
        lines.append("Triggers:")
        for trig in triggers_el:
            trig_class = trig.tag.split(".")[-1] if "." in trig.tag else trig.tag
            spec_el = trig.find("spec")
            if spec_el is not None and spec_el.text:
                lines.append(f"  {trig_class}: {spec_el.text.strip()}")
            else:
                lines.append(f"  {trig_class}")
        lines.append("")

    # Agent label
    label_el = root.find(".//assignedNode")
    if label_el is not None and label_el.text:
        lines.append(f"Agent label: {label_el.text.strip()}")

    # Pipeline definition
    definition = root.find(".//definition")
    if definition is not None:
        def_class = definition.get("class", "")
        if "CpsScmFlowDefinition" in def_class:
            script_path = definition.find(".//scriptPath")
            lines.append(f"Pipeline: Jenkinsfile from SCM")
            if script_path is not None and script_path.text:
                lines.append(f"  Path: {script_path.text.strip()}")
        elif "CpsFlowDefinition" in def_class:
            script = definition.find(".//script")
            lines.append("Pipeline: Inline script")
            if script is not None and script.text:
                script_lines = script.text.strip().splitlines()
                preview = "\n".join(f"  {l}" for l in script_lines[:80])
                lines.append(preview)
                if len(script_lines) > 80:
                    lines.append(f"  ... ({len(script_lines) - 80} more lines)")

    return "\n".join(lines)


@mcp.tool
def get_queue_info(job_name: str = "") -> str:
    """Show the Jenkins build queue.  Reports why builds are waiting.

    Args:
        job_name: Filter to a specific job (empty = show global queue).
    """
    try:
        items = jenkins_api.get_queue(job_filter=job_name)
    except Exception as exc:
        return _handle_error(exc, "get_queue_info")
    finally:
        time.sleep(TOOL_DELAY)

    if not items:
        return "Queue is empty." if not job_name else f"No queued builds for '{job_name}'."

    lines = [f"Build queue ({len(items)} items):\n"]
    for item in items:
        age_s = round((time.time() * 1000 - item["in_queue_since_ms"]) / 1000) if item["in_queue_since_ms"] else 0
        age_str = f"{age_s}s" if age_s < 120 else f"{age_s // 60}m"
        stuck = " [STUCK]" if item.get("stuck") else ""
        lines.append(f"  {item['task_name']}{stuck} (queued {age_str})")
        if item.get("why"):
            lines.append(f"    Why: {item['why'][:200]}")
    return "\n".join(lines)


_OS_NOISE_VARS = frozenset({
    "PATH", "HOME", "SHELL", "USER", "LANG", "TERM", "SHLVL", "PWD",
    "OLDPWD", "HOSTNAME", "LOGNAME", "MAIL", "MANPATH", "DISPLAY",
    "LESS", "LS_COLORS", "_", "TMPDIR", "TMP", "TEMP", "COLORTERM",
    "EDITOR", "VISUAL", "PAGER",
})
_OS_NOISE_PREFIXES = ("LC_", "XDG_", "DBUS_", "SSH_", "GPG_", "QT_", "GTK_")


@mcp.tool
def get_build_environment(job_name: str, build_number: int) -> str:
    """Show CI-relevant environment variables for a build (requires
    EnvInject plugin).  OS noise variables are filtered out.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        env_map = jenkins_api.get_injected_env_vars(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_build_environment")
    finally:
        time.sleep(TOOL_DELAY)

    if env_map is None:
        return (
            "Environment variables unavailable — the EnvInject plugin is "
            "not installed on this Jenkins instance."
        )

    filtered = {}
    for k, v in env_map.items():
        if k in _OS_NOISE_VARS:
            continue
        if any(k.startswith(p) for p in _OS_NOISE_PREFIXES):
            continue
        filtered[k] = v

    if not filtered:
        return "No CI-relevant environment variables found."

    lines = [f"Environment for {job_name} #{build_number} ({len(filtered)} vars):\n"]
    for k in sorted(filtered)[:60]:
        lines.append(f"  {k} = {filtered[k]}")
    if len(filtered) > 60:
        lines.append(f"\n  [{len(filtered) - 60} more variables not shown]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Infrastructure Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_node_list(label_filter: str = "") -> str:
    """List all Jenkins agents with status, labels, executors, and disk space.

    Args:
        label_filter: Only show nodes with this label (empty = all nodes).
    """
    try:
        nodes = jenkins_api.get_all_nodes()
    except Exception as exc:
        return _handle_error(exc, "get_node_list")
    finally:
        time.sleep(TOOL_DELAY)

    if label_filter:
        lf = label_filter.lower()
        nodes = [n for n in nodes if any(lf in lbl.lower() for lbl in n.get("labels", []))]

    if not nodes:
        msg = f"No nodes found matching label '{label_filter}'." if label_filter else "No nodes found."
        return msg

    lines = [f"Jenkins agents ({len(nodes)}):\n"]
    lines.append(f"  {'Name':<25} {'Status':<10} {'Executors':>10}  {'Disk':>10}  Labels")
    lines.append(f"  {'-'*25} {'-'*10} {'-'*10}  {'-'*10}  {'-'*20}")
    for n in nodes:
        status = "ONLINE" if n["online"] else "OFFLINE"
        disk = f"{n['disk_gb']} GB" if n.get("disk_gb") is not None else "n/a"
        labels = ", ".join(n.get("labels", [])[:5]) or "none"
        execs = f"{n.get('executors', 0)}"
        lines.append(f"  {n['name']:<25} {status:<10} {execs:>10}  {disk:>10}  {labels}")

    return "\n".join(lines)


@mcp.tool
def get_pipeline_flow_nodes(job_name: str, build_number: int, stage_name: str) -> str:
    """Drill into a pipeline stage to see parallel branches and step-level detail.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
        stage_name: Name of the stage to inspect.
    """
    try:
        stages = jenkins_api.get_pipeline_stages(job_name, build_number)
    except Exception as exc:
        return _handle_error(exc, "get_pipeline_flow_nodes")

    if stages is None:
        return "Not a Pipeline job."
    if not stages:
        return "No stages recorded."

    match = _resolve_stage(stages, stage_name)
    if isinstance(match, str):
        time.sleep(TOOL_DELAY)
        return match

    try:
        detail = jenkins_api.get_flow_node_detail(job_name, build_number, match["id"])
    except Exception as exc:
        return _handle_error(exc, "get_pipeline_flow_nodes")
    finally:
        time.sleep(TOOL_DELAY)

    if detail is None:
        return f"No flow-node detail available for stage '{match['name']}'."

    nodes = detail.get("stage_flow_nodes", [])
    if not nodes:
        return f"Stage '{match['name']}' has no child flow nodes (single-threaded stage)."

    lines = [f"Flow nodes in stage '{match['name']}':\n"]
    lines.append(f"  {'Node':<30} {'Status':<15} {'Duration':>10}")
    lines.append(f"  {'-'*30} {'-'*15} {'-'*10}")
    for fn in nodes:
        lines.append(f"  {fn['name']:<30} {fn['status']:<15} {fn['duration_s']:>9.1f}s")

    return "\n".join(lines)


@mcp.tool
def get_node_status(node_name: str) -> str:
    """Check agent online status and disk space (< 10 GB = warning).

    Args:
        node_name: Agent name (use 'master' or 'built-in' for controller).
    """
    try:
        info = jenkins_api.get_node_info(node_name)
    except Exception as exc:
        return _handle_error(exc, "get_node_status")
    finally:
        time.sleep(TOOL_DELAY)

    status = "ONLINE" if info["online"] else "OFFLINE"
    lines = [
        f"Node:   {info['display_name']}",
        f"Status: {status}",
    ]

    if not info["online"] and info["offline_reason"]:
        lines.append(f"Reason: {info['offline_reason']}")

    if info["disk_space_gb"] is not None:
        disk_str = f"{info['disk_space_gb']} GB free"
        if info["disk_warning"]:
            disk_str += " [WARNING: low disk space]"
        lines.append(f"Disk:   {disk_str}")
    else:
        lines.append("Disk:   unavailable (monitor data not returned)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bundle Tools — multi-call composites that call API wrappers directly
# ---------------------------------------------------------------------------

_BUNDLE_PACING = 0.05  # seconds between sub-calls to avoid hammering Jenkins

_INVESTIGATE_BUDGET = {
    "error": 150,
    "test": 80,
    "commits": 25,
    "params": 15,
    "trend": 12,
}
_INVESTIGATE_HARD_CAP = 400
_INVESTIGATE_MAX_TESTS = 10
_INVESTIGATE_MAX_COMMITS = 8


@mcp.tool
def investigate_build_failure(job_name: str, selector: str = "last_failed") -> str:
    """One-call RCA context for a failing build.  Returns build info, failed
    stages, error log extract, test failures, commits, parameters, and trend.
    Start here instead of calling multiple tools individually.

    Args:
        job_name: Jenkins job name.
        selector: Which build to inspect (last_failed, last, last_stable, last_success).
    """
    try:
        build_data = jenkins_api.get_named_build(job_name, selector)
    except Exception as exc:
        time.sleep(TOOL_DELAY)
        return _handle_error(exc, "investigate_build_failure")

    if not build_data:
        time.sleep(TOOL_DELAY)
        return f"No '{selector}' build found for job '{job_name}'."

    build_number = build_data.get("number")
    if not build_number:
        time.sleep(TOOL_DELAY)
        return f"Could not determine build number from '{selector}' for '{job_name}'."

    sections: list[str] = []
    actions = build_data.get("actions") or []

    # --- Build Info ---
    triggered_by = jenkins_api.get_build_trigger(actions)
    duration_s = round((build_data.get("duration") or 0) / 1000, 1)
    ts = build_data.get("timestamp")
    started = (
        datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        if ts else "unknown"
    )
    sections.append(
        f"=== BUILD INFO ===\n"
        f"Job: {job_name} #{build_number}\n"
        f"Result: {build_data.get('result', 'IN_PROGRESS')}\n"
        f"Started: {started}\n"
        f"Duration: {duration_s}s\n"
        f"Agent: {build_data.get('builtOn') or 'controller'}\n"
        f"Trigger: {triggered_by}"
    )

    # --- Pipeline Stages ---
    time.sleep(_BUNDLE_PACING)
    try:
        stages = jenkins_api.get_pipeline_stages(job_name, build_number)
    except Exception:
        stages = None

    if stages:
        stage_lines = ["=== PIPELINE STAGES ==="]
        for s in stages:
            marker = " <<<" if s["status"] not in ("SUCCESS", "NOT_EXECUTED") else ""
            stage_lines.append(f"  {s['name']:<30} {s['status']:<15} {s['duration_s']:>8.1f}s{marker}")
        sections.append("\n".join(stage_lines))

    # --- Error Summary ---
    time.sleep(_BUNDLE_PACING)
    try:
        console_text = jenkins_api.get_console_text(job_name, build_number)
        error_extract = log_parser.get_error_log(
            console_text,
            max_lines=_INVESTIGATE_BUDGET["error"],
            hard_limit=_INVESTIGATE_BUDGET["error"] + 20,
            include_head=True,
            include_tail=True,
        )
        sections.append(f"=== ERROR SUMMARY ===\n{error_extract}")
    except Exception:
        sections.append("=== ERROR SUMMARY ===\n[Console log unavailable]")

    # --- Test Failures ---
    time.sleep(_BUNDLE_PACING)
    try:
        report = jenkins_api.get_test_report(job_name, build_number)
    except Exception:
        report = None

    if report is not None:
        test_lines = [
            f"=== TEST FAILURES ===",
            f"Summary: {report['fail_count']} failed, {report['pass_count']} passed, {report['skip_count']} skipped.",
        ]
        for test in report["failing_tests"][:_INVESTIGATE_MAX_TESTS]:
            test_lines.append(f"  FAIL: {test['class_name']}.{test['test_name']}")
            if test["error_details"]:
                test_lines.append(f"    {test['error_details'][:200]}")
        if len(report["failing_tests"]) > _INVESTIGATE_MAX_TESTS:
            test_lines.append(f"  ... and {len(report['failing_tests']) - _INVESTIGATE_MAX_TESTS} more failing tests")
        sections.append("\n".join(test_lines))

    # --- SCM Changes (reuses data from build_data) ---
    commits = scm.extract_changesets(build_data)
    if commits:
        commit_lines = [f"=== SCM CHANGES ({len(commits)} commits) ==="]
        for c in commits[:_INVESTIGATE_MAX_COMMITS]:
            commit_lines.append(f"  [{c['commit_id']}] {c['author']}: {c['message'][:100]}")
        if len(commits) > _INVESTIGATE_MAX_COMMITS:
            commit_lines.append(f"  ... and {len(commits) - _INVESTIGATE_MAX_COMMITS} more commits")
        sections.append("\n".join(commit_lines))

    # --- Parameters (reuses data from build_data) ---
    params = jenkins_api.extract_parameters(actions)
    if params:
        param_lines = ["=== PARAMETERS ==="]
        for p in params[:_INVESTIGATE_BUDGET["params"]]:
            param_lines.append(f"  {p['name']} = {p['value']}")
        sections.append("\n".join(param_lines))

    # --- Build Trend ---
    time.sleep(_BUNDLE_PACING)
    try:
        history = jenkins_api.get_build_history(job_name, 10)
    except Exception:
        history = []

    if history:
        results = [b["result"] for b in history]
        consecutive_fail = 0
        for r in results:
            if r in ("FAILURE", "ABORTED", "UNSTABLE"):
                consecutive_fail += 1
            else:
                break

        trend_lines = ["=== RECENT TREND ==="]
        seq = " ".join("F" if r in ("FAILURE", "ABORTED") else "U" if r == "UNSTABLE" else "P" for r in results)
        trend_lines.append(f"  Last {len(history)}: {seq}")

        if consecutive_fail == 0:
            trend_lines.append("  Latest build passed.")
        elif consecutive_fail == len(history):
            trend_lines.append(f"  All {len(history)} recent builds failed — long-standing breakage.")
        else:
            trend_lines.append(
                f"  Last {consecutive_fail} failed; build #{history[consecutive_fail]['number']} was last pass."
            )
        sections.append("\n".join(trend_lines))

    time.sleep(TOOL_DELAY)

    result = "\n\n".join(sections)
    result_lines = result.splitlines()
    if len(result_lines) > _INVESTIGATE_HARD_CAP:
        result = "\n".join(result_lines[:_INVESTIGATE_HARD_CAP])
        result += f"\n[Output truncated at {_INVESTIGATE_HARD_CAP} lines]"
    return result


_COMPARE_MAX_GAP_BUILDS = 10
_COMPARE_HARD_CAP = 200


@mcp.tool
def compare_failing_vs_passing(job_name: str, failing_build: int = 0) -> str:
    """Compare the last failing build against the last passing build to find
    what changed (commits, parameters, agent, trigger).

    Args:
        job_name: Jenkins job name.
        failing_build: Build number of the failing build.  0 = auto-detect last failed.
    """
    try:
        if failing_build == 0:
            build_data = jenkins_api.get_named_build(job_name, "last_failed")
            if not build_data:
                time.sleep(TOOL_DELAY)
                return f"No failed build found for job '{job_name}'."
            failing_build = build_data["number"]

        time.sleep(_BUNDLE_PACING)
        history = jenkins_api.get_build_history(job_name, 25)
    except Exception as exc:
        time.sleep(TOOL_DELAY)
        return _handle_error(exc, "compare_failing_vs_passing")

    if not history:
        time.sleep(TOOL_DELAY)
        return f"No build history found for job '{job_name}'."

    last_pass = None
    fail_idx = None
    for i, b in enumerate(history):
        if b["number"] == failing_build:
            fail_idx = i
        if fail_idx is not None and b["result"] == "SUCCESS":
            last_pass = b
            break

    if last_pass is None:
        time.sleep(TOOL_DELAY)
        return (
            f"No passing build found in the last {len(history)} builds for '{job_name}'. "
            "This appears to be a long-standing breakage."
        )

    sections: list[str] = []
    sections.append(
        f"=== COMPARISON ===\n"
        f"Failing: #{failing_build}\n"
        f"Last pass: #{last_pass['number']}\n"
        f"Gap: {failing_build - last_pass['number'] - 1} build(s) between them"
    )

    # Fetch full build data for both
    time.sleep(_BUNDLE_PACING)
    try:
        fail_data = jenkins_api.get_build(job_name, failing_build)
    except Exception:
        fail_data = {}
    time.sleep(_BUNDLE_PACING)
    try:
        pass_data = jenkins_api.get_build(job_name, last_pass["number"])
    except Exception:
        pass_data = {}

    # Parameter diff
    fail_params = {p["name"]: p["value"] for p in jenkins_api.extract_parameters(fail_data.get("actions") or [])}
    pass_params = {p["name"]: p["value"] for p in jenkins_api.extract_parameters(pass_data.get("actions") or [])}

    all_keys = set(fail_params) | set(pass_params)
    changed = []
    for k in sorted(all_keys):
        fv, pv = fail_params.get(k), pass_params.get(k)
        if fv != pv:
            if pv is None:
                changed.append(f"  + {k} = {fv}  (added)")
            elif fv is None:
                changed.append(f"  - {k} = {pv}  (removed)")
            else:
                changed.append(f"  ~ {k}: {pv} → {fv}")

    if changed:
        sections.append("=== PARAMETER DIFF ===\n" + "\n".join(changed))

    # Agent / trigger diff
    fail_agent = fail_data.get("builtOn") or "controller"
    pass_agent = pass_data.get("builtOn") or "controller"
    fail_trigger = jenkins_api.get_build_trigger(fail_data.get("actions") or [])
    pass_trigger = jenkins_api.get_build_trigger(pass_data.get("actions") or [])

    infra_lines = []
    if fail_agent != pass_agent:
        infra_lines.append(f"  Agent: {pass_agent} → {fail_agent}")
    if fail_trigger != pass_trigger:
        infra_lines.append(f"  Trigger: {pass_trigger} → {fail_trigger}")
    if infra_lines:
        sections.append("=== INFRA DIFF ===\n" + "\n".join(infra_lines))

    # Duration diff
    fail_dur = round((fail_data.get("duration") or 0) / 1000, 1)
    pass_dur = round((pass_data.get("duration") or 0) / 1000, 1)
    if abs(fail_dur - pass_dur) > 10:
        sections.append(
            f"=== DURATION DIFF ===\n"
            f"  Passing #{last_pass['number']}: {pass_dur}s\n"
            f"  Failing #{failing_build}: {fail_dur}s\n"
            f"  Delta: {fail_dur - pass_dur:+.1f}s"
        )

    # Cumulative commits in the gap
    gap_start = last_pass["number"] + 1
    gap_end = failing_build
    gap_builds_to_fetch = list(range(gap_start, gap_end + 1))

    if len(gap_builds_to_fetch) > _COMPARE_MAX_GAP_BUILDS:
        first_n = gap_builds_to_fetch[:3]
        last_n = gap_builds_to_fetch[-3:]
        omitted = len(gap_builds_to_fetch) - 6
        gap_builds_to_fetch = first_n + last_n
        gap_note = f"  [{omitted} intermediate builds omitted]\n"
    else:
        gap_note = ""

    all_commits: list[str] = []
    for bn in gap_builds_to_fetch:
        time.sleep(_BUNDLE_PACING)
        try:
            bd = jenkins_api.get_build(job_name, bn)
            commits = scm.extract_changesets(bd)
            for c in commits:
                all_commits.append(f"  #{bn} [{c['commit_id']}] {c['author']}: {c['message'][:100]}")
        except Exception:
            all_commits.append(f"  #{bn} [build data unavailable]")

    if all_commits:
        commit_section = "=== CUMULATIVE COMMITS ===\n" + gap_note + "\n".join(all_commits)
        sections.append(commit_section)
    elif gap_start <= gap_end:
        sections.append("=== CUMULATIVE COMMITS ===\nNo SCM changes recorded in gap builds.")

    time.sleep(TOOL_DELAY)

    result = "\n\n".join(sections)
    result_lines = result.splitlines()
    if len(result_lines) > _COMPARE_HARD_CAP:
        result = "\n".join(result_lines[:_COMPARE_HARD_CAP])
        result += f"\n[Output truncated at {_COMPARE_HARD_CAP} lines]"
    return result


_DEEP_DIVE_MAX_TESTS = 10
_DEEP_DIVE_HISTORY_DEPTH = 5
_DEEP_DIVE_HARD_CAP = 250


@mcp.tool
def deep_dive_test_failures(job_name: str, build_number: int) -> str:
    """Analyze failing tests: when each first broke and which commit likely caused it.
    Checks previous builds to find the regression point.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        report = jenkins_api.get_test_report(job_name, build_number)
    except Exception as exc:
        time.sleep(TOOL_DELAY)
        return _handle_error(exc, "deep_dive_test_failures")

    if report is None:
        time.sleep(TOOL_DELAY)
        return "No test report found for this build."

    if not report["failing_tests"]:
        time.sleep(TOOL_DELAY)
        return (
            f"Test summary: {report['fail_count']} failed, {report['pass_count']} passed — "
            "but no individual test failure details recorded."
        )

    # Prioritize REGRESSION-status tests, then take up to cap
    failing = report["failing_tests"][:_DEEP_DIVE_MAX_TESTS]

    # Fetch previous builds' test reports (reuse across all tests)
    prev_reports: list[tuple[int, dict | None]] = []
    time.sleep(_BUNDLE_PACING)
    try:
        history = jenkins_api.get_build_history(job_name, _DEEP_DIVE_HISTORY_DEPTH + 1)
    except Exception:
        history = []

    prev_builds = [b for b in history if b["number"] < build_number][:_DEEP_DIVE_HISTORY_DEPTH]

    for b in prev_builds:
        time.sleep(_BUNDLE_PACING)
        try:
            r = jenkins_api.get_test_report(job_name, b["number"])
        except Exception:
            r = None
        prev_reports.append((b["number"], r))

    # --- Pass 1: determine regression build for each test ---
    test_analysis: list[dict] = []
    regression_builds_needed: set[int] = set()

    for test in failing:
        key = f"{test['class_name']}.{test['test_name']}"

        run_history: list[str] = []
        for bn, r in prev_reports:
            if r is None:
                run_history.append(f"#{bn}:?")
                continue
            test_found = any(
                f"{ft['class_name']}.{ft['test_name']}" == key
                for ft in r.get("failing_tests", [])
            )
            run_history.append(f"#{bn}:{'FAIL' if test_found else 'PASS'}")

        run_history.reverse()
        run_history.append(f"#{build_number}:FAIL")

        # Walk chronologically to find the most recent PASS→FAIL transition
        regression_build: int | None = None
        last_pass_seen = False
        for entry in run_history:
            bn_str, result = entry.split(":")
            bn_val = int(bn_str.lstrip("#"))
            if result == "PASS":
                last_pass_seen = True
            elif result == "FAIL":
                if last_pass_seen:
                    regression_build = bn_val
                last_pass_seen = False

        if regression_build is not None and regression_build != build_number:
            regression_builds_needed.add(regression_build)

        test_analysis.append({
            "test": test, "key": key,
            "run_history": run_history,
            "regression_build": regression_build,
        })

    # Fetch build data only for identified regression builds
    regression_build_data: dict[int, dict] = {}
    for bn in regression_builds_needed:
        time.sleep(_BUNDLE_PACING)
        try:
            regression_build_data[bn] = jenkins_api.get_build(job_name, bn)
        except Exception:
            pass

    # --- Pass 2: format output ---
    sections: list[str] = []
    sections.append(
        f"=== TEST FAILURE ANALYSIS ===\n"
        f"Build: {job_name} #{build_number}\n"
        f"Total: {report['fail_count']} failed, {report['pass_count']} passed, {report['skip_count']} skipped\n"
        f"Analyzing top {len(failing)} failing tests:"
    )

    for entry in test_analysis:
        test = entry["test"]
        key = entry["key"]
        run_history = entry["run_history"]
        regression_build = entry["regression_build"]

        test_lines = [f"\n  TEST: {key}"]
        if test["error_details"]:
            test_lines.append(f"  Error: {test['error_details'][:150]}")

        test_lines.append(f"  History: {' → '.join(run_history)}")

        if regression_build is None:
            if prev_builds:
                test_lines.append(
                    f"  Status: Persistent failure (failing in all {len(prev_builds)} prior builds checked)"
                )
            else:
                test_lines.append("  Status: No prior build history available to determine regression point")
        elif regression_build == build_number:
            test_lines.append("  Status: NEW failure (passed in all prior builds)")
        else:
            test_lines.append(f"  Status: Regression started at build #{regression_build}")

        if regression_build is not None and regression_build in regression_build_data:
            commits = scm.extract_changesets(regression_build_data[regression_build])
            if commits:
                test_lines.append(f"  Suspect commit (build #{regression_build}):")
                for c in commits[:3]:
                    test_lines.append(f"    [{c['commit_id']}] {c['author']}: {c['message'][:80]}")

        sections.append("\n".join(test_lines))

    if len(report["failing_tests"]) > _DEEP_DIVE_MAX_TESTS:
        sections.append(f"\n[{len(report['failing_tests']) - _DEEP_DIVE_MAX_TESTS} more failing tests not analyzed]")

    time.sleep(TOOL_DELAY)

    result = "\n".join(sections)
    result_lines = result.splitlines()
    if len(result_lines) > _DEEP_DIVE_HARD_CAP:
        result = "\n".join(result_lines[:_DEEP_DIVE_HARD_CAP])
        result += f"\n[Output truncated at {_DEEP_DIVE_HARD_CAP} lines]"
    return result


_FLAKY_HARD_CAP = 150
_FLAKY_MAX_STAGE_ENRICHMENT = 10


@mcp.tool
def analyze_flaky_job(job_name: str, window: int = 25) -> str:
    """Analyze a job for flakiness: score, result pattern, and failure clustering
    by stage, node, and time of day.

    Args:
        job_name: Jenkins job name.
        window: Number of recent builds to analyze (default 25, max 25).
    """
    try:
        history = jenkins_api.get_build_history(job_name, min(window, 25))
    except Exception as exc:
        time.sleep(TOOL_DELAY)
        return _handle_error(exc, "analyze_flaky_job")

    if len(history) < 3:
        time.sleep(TOOL_DELAY)
        return f"Not enough build history for flakiness analysis ({len(history)} builds)."

    results = [b["result"] for b in history]
    transitions = sum(1 for i in range(1, len(results)) if results[i] != results[i - 1])
    flakiness = round(transitions / (len(results) - 1), 2)

    seq = " ".join("F" if r in ("FAILURE", "ABORTED") else "U" if r == "UNSTABLE" else "P" for r in results)
    fail_count = sum(1 for r in results if r in ("FAILURE", "ABORTED", "UNSTABLE"))

    sections: list[str] = []
    sections.append(
        f"=== FLAKINESS ANALYSIS ===\n"
        f"Job: {job_name}\n"
        f"Window: {len(history)} builds\n"
        f"Flakiness score: {flakiness} (0=stable, 1=alternates every build)\n"
        f"Failures: {fail_count}/{len(history)}\n"
        f"Sequence: {seq}"
    )

    # Clustering by node
    node_stats: dict[str, dict] = {}
    for b in history:
        agent = b.get("agent", "controller")
        if agent not in node_stats:
            node_stats[agent] = {"runs": 0, "failures": 0}
        node_stats[agent]["runs"] += 1
        if b["result"] in ("FAILURE", "ABORTED", "UNSTABLE"):
            node_stats[agent]["failures"] += 1

    if len(node_stats) > 1:
        global_rate = fail_count / len(history) if history else 0
        node_lines = ["=== BY NODE ==="]
        for agent, stats in sorted(node_stats.items(), key=lambda x: -x[1]["failures"]):
            rate = stats["failures"] / stats["runs"] if stats["runs"] else 0
            marker = " <<<" if rate > global_rate * 1.5 and stats["failures"] > 1 else ""
            node_lines.append(
                f"  {agent:<25} {stats['failures']}/{stats['runs']} failed "
                f"({rate:.0%} vs avg {global_rate:.0%}){marker}"
            )
        sections.append("\n".join(node_lines))

    # Clustering by time
    biz_runs = biz_fail = off_runs = off_fail = 0
    for b in history:
        ts = b.get("timestamp")
        if not ts:
            continue
        hour = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour
        if 8 <= hour < 18:
            biz_runs += 1
            if b["result"] in ("FAILURE", "ABORTED", "UNSTABLE"):
                biz_fail += 1
        else:
            off_runs += 1
            if b["result"] in ("FAILURE", "ABORTED", "UNSTABLE"):
                off_fail += 1

    if biz_runs > 0 and off_runs > 0:
        biz_rate = biz_fail / biz_runs
        off_rate = off_fail / off_runs
        sections.append(
            f"=== BY TIME ===\n"
            f"  Business hours (08-18 UTC): {biz_fail}/{biz_runs} ({biz_rate:.0%})\n"
            f"  Off hours: {off_fail}/{off_runs} ({off_rate:.0%})"
        )

    # Stage enrichment (pipeline jobs only, limited calls)
    failing_builds = [b for b in history if b["result"] in ("FAILURE", "ABORTED", "UNSTABLE")]
    is_pipeline = None
    stage_failures: dict[str, int] = {}

    for fb in failing_builds[:_FLAKY_MAX_STAGE_ENRICHMENT]:
        time.sleep(_BUNDLE_PACING)
        try:
            stages = jenkins_api.get_pipeline_stages(job_name, fb["number"])
        except Exception:
            stages = None

        if stages is None:
            if is_pipeline is None:
                is_pipeline = False
            break
        is_pipeline = True
        for s in stages:
            if s["status"] not in ("SUCCESS", "NOT_EXECUTED"):
                stage_failures[s["name"]] = stage_failures.get(s["name"], 0) + 1

    if stage_failures:
        stage_lines = ["=== BY STAGE ==="]
        for name, count in sorted(stage_failures.items(), key=lambda x: -x[1]):
            stage_lines.append(f"  {name:<30} failed {count}x")
        sections.append("\n".join(stage_lines))

    # Verdict
    verdict_parts = []
    if len(node_stats) > 1:
        worst_agent = max(node_stats.items(), key=lambda x: x[1]["failures"] / max(x[1]["runs"], 1))
        worst_rate = worst_agent[1]["failures"] / max(worst_agent[1]["runs"], 1)
        if worst_rate > (fail_count / len(history)) * 1.5 and worst_agent[1]["failures"] > 1:
            verdict_parts.append(
                f"Strong node correlation: {worst_agent[0]} fails {worst_rate:.0%} "
                f"vs average {fail_count / len(history):.0%}"
            )
    if stage_failures:
        top_stage = max(stage_failures.items(), key=lambda x: x[1])
        if top_stage[1] > fail_count * 0.5:
            verdict_parts.append(f"Concentrated in stage '{top_stage[0]}' ({top_stage[1]}/{fail_count} failures)")

    if not verdict_parts:
        verdict_parts.append("No clear pattern — failures distributed across nodes/stages/times")

    sections.append("=== VERDICT ===\n" + "\n".join(f"  {v}" for v in verdict_parts))

    time.sleep(TOOL_DELAY)

    result = "\n\n".join(sections)
    result_lines = result.splitlines()
    if len(result_lines) > _FLAKY_HARD_CAP:
        result = "\n".join(result_lines[:_FLAKY_HARD_CAP])
        result += f"\n[Output truncated at {_FLAKY_HARD_CAP} lines]"
    return result


_INFRA_HARD_CAP = 100


@mcp.tool
def diagnose_infrastructure_issue(job_name: str, build_number: int) -> str:
    """Check whether a build failure is node-related: node health, disk, and
    per-node failure correlation from recent history.  Uses exactly 3 API calls.

    Args:
        job_name: Jenkins job name.
        build_number: Build number.
    """
    try:
        build = jenkins_api.get_build(job_name, build_number)
    except Exception as exc:
        time.sleep(TOOL_DELAY)
        return _handle_error(exc, "diagnose_infrastructure_issue")

    agent = build.get("builtOn") or "controller"

    sections: list[str] = []
    sections.append(
        f"=== INFRASTRUCTURE DIAGNOSIS ===\n"
        f"Build: {job_name} #{build_number}\n"
        f"Result: {build.get('result', '?')}\n"
        f"Agent: {agent}"
    )

    # Node health
    time.sleep(_BUNDLE_PACING)
    try:
        node_info = jenkins_api.get_node_info(agent)
        status = "ONLINE" if node_info["online"] else "OFFLINE"
        node_lines = [f"=== NODE HEALTH ===", f"  Status: {status}"]
        if not node_info["online"] and node_info["offline_reason"]:
            node_lines.append(f"  Reason: {node_info['offline_reason']}")
        if node_info["disk_space_gb"] is not None:
            disk_str = f"{node_info['disk_space_gb']} GB"
            if node_info["disk_warning"]:
                disk_str += " [WARNING: LOW]"
            node_lines.append(f"  Disk: {disk_str}")
        sections.append("\n".join(node_lines))
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            sections.append(
                "=== NODE HEALTH ===\n"
                f"  Node '{agent}' no longer exists (ephemeral/cloud agent). "
                "Health check unavailable, but correlation data below is still valid."
            )
        else:
            sections.append("=== NODE HEALTH ===\n  [Node info unavailable]")

    # Failure correlation from history (builtOn already included)
    time.sleep(_BUNDLE_PACING)
    try:
        history = jenkins_api.get_build_history(job_name, 20)
    except Exception:
        history = []

    if history:
        node_stats: dict[str, dict] = {}
        total_fail = 0
        for b in history:
            a = b.get("agent", "controller")
            if a not in node_stats:
                node_stats[a] = {"runs": 0, "failures": 0}
            node_stats[a]["runs"] += 1
            if b["result"] in ("FAILURE", "ABORTED", "UNSTABLE"):
                node_stats[a]["failures"] += 1
                total_fail += 1

        global_rate = total_fail / len(history) if history else 0

        corr_lines = ["=== FAILURE CORRELATION ==="]
        for a, stats in sorted(node_stats.items(), key=lambda x: -x[1]["failures"]):
            rate = stats["failures"] / stats["runs"] if stats["runs"] else 0
            marker = ""
            if a == agent:
                marker = " ← this build"
            if rate > global_rate * 1.5 and stats["failures"] > 1:
                marker += " [HIGH]"
            corr_lines.append(
                f"  {a:<25} {stats['failures']}/{stats['runs']} ({rate:.0%}){marker}"
            )
        corr_lines.append(f"\n  Fleet average: {global_rate:.0%}")
        sections.append("\n".join(corr_lines))

        # Verdict
        this_stats = node_stats.get(agent, {"runs": 0, "failures": 0})
        this_rate = this_stats["failures"] / this_stats["runs"] if this_stats["runs"] else 0

        if this_rate > global_rate * 2 and this_stats["failures"] > 1:
            verdict = (
                f"Node '{agent}' has a {this_rate:.0%} failure rate vs fleet average {global_rate:.0%} "
                f"— likely infrastructure-related."
            )
        elif not node_info.get("online", True) if 'node_info' in dir() else False:
            verdict = f"Node '{agent}' is OFFLINE — likely infrastructure-related."
        else:
            verdict = f"Node '{agent}' failure rate ({this_rate:.0%}) is within normal range — likely not infra-related."

        sections.append(f"=== VERDICT ===\n  {verdict}")

    time.sleep(TOOL_DELAY)

    result = "\n\n".join(sections)
    result_lines = result.splitlines()
    if len(result_lines) > _INFRA_HARD_CAP:
        result = "\n".join(result_lines[:_INFRA_HARD_CAP])
        result += f"\n[Output truncated at {_INFRA_HARD_CAP} lines]"
    return result


_SEARCH_ACROSS_MAX_JOBS = 50
_SEARCH_ACROSS_MAX_MATCHES_PER_JOB = 5
_SEARCH_ACROSS_TOTAL_MATCHES = 30
_SEARCH_ACROSS_TAIL_BYTES = 500_000
_SEARCH_ACROSS_WORKERS = 8
_SEARCH_ACROSS_OUTPUT_CAP = 300


@mcp.tool
def search_across_jobs(
    folder: str,
    pattern: str,
    is_regex: bool = False,
    build_selector: str = "last",
    filter_status: str = "all",
    recursive: bool = False,
    context_lines: int = 2,
) -> str:
    """Search console logs across all jobs in a folder for a specific error
    pattern.  Returns matching lines grouped by job.  Searches the tail of
    each log (~500 KB) concurrently to minimise data transfer and latency.

    Args:
        folder: Jenkins folder to search (empty string for root).
        pattern: Search string or regex to find in console logs.
        is_regex: Treat pattern as regex (default: literal match).
        build_selector: Which build per job: "last" or "last_failed".
        filter_status: Pre-filter jobs: "all", "failing", or "unstable_and_failing".
        recursive: Recurse into subfolders (default false).
        context_lines: Lines of context around each match (default 2).
    """
    # --- Step 1: Discover jobs (1 API call, +1 per subfolder if recursive) ---
    try:
        jobs = jenkins_api.get_folder_jobs(folder, include_last_failed=True)
    except Exception as exc:
        time.sleep(TOOL_DELAY)
        return _handle_error(exc, "search_across_jobs")

    real_jobs = [j for j in jobs if j.get("_class", "") not in _FOLDER_CLASSES]
    subfolders = [j for j in jobs if j.get("_class", "") in _FOLDER_CLASSES]

    if recursive and subfolders:
        for sf in subfolders[:10]:
            try:
                sub_path = f"{folder}/{sf['name']}" if folder else sf["name"]
                sub_jobs = jenkins_api.get_folder_jobs(sub_path, include_last_failed=True)
                for sj in sub_jobs:
                    if sj.get("_class", "") not in _FOLDER_CLASSES:
                        sj["name"] = f"{sf['name']}/{sj['name']}"
                        real_jobs.append(sj)
            except Exception:
                pass

    # Apply status filter
    if filter_status == "failing":
        real_jobs = [j for j in real_jobs if j.get("color", "").startswith("red")]
    elif filter_status == "unstable_and_failing":
        real_jobs = [
            j for j in real_jobs
            if j.get("color", "").startswith(("red", "yellow"))
        ]

    # Prioritise failing > unstable > rest
    def _color_priority(j):
        c = j.get("color", "")
        if c.startswith("red"):
            return 0
        if c.startswith("yellow"):
            return 1
        return 2

    real_jobs.sort(key=_color_priority)
    total_jobs_found = len(real_jobs)

    # Resolve build number per job
    candidates: list[tuple[str, int, str]] = []
    for j in real_jobs[:_SEARCH_ACROSS_MAX_JOBS]:
        job_path = f"{folder}/{j['name']}" if folder else j["name"]
        if build_selector == "last_failed":
            bn = j.get("last_failed_build_number")
            result_str = "FAILURE"
        else:
            bn = j.get("last_build_number")
            result_str = j.get("last_result") or "?"
        if bn is not None:
            candidates.append((job_path, bn, result_str))

    if not candidates:
        time.sleep(TOOL_DELAY)
        qualifier = f" (filter: {filter_status})" if filter_status != "all" else ""
        return f"No jobs with matching builds found in '{folder or '(root)'}'{qualifier}."

    # Compile search pattern
    if is_regex:
        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error:
            compiled = re.compile(re.escape(pattern), re.IGNORECASE)
    else:
        compiled = re.compile(re.escape(pattern), re.IGNORECASE)

    # --- Step 2: Concurrent log search ---
    state = {"match_count": 0}
    lock = threading.Lock()
    stop_event = threading.Event()

    def _search_one(job_path: str, build_number: int, build_result: str) -> dict:
        if stop_event.is_set():
            return {"job": job_path, "build": build_number, "skipped": True}

        try:
            text = jenkins_api.get_console_text_tail(
                job_path, build_number, _SEARCH_ACROSS_TAIL_BYTES,
            )
        except Exception as exc:
            return {
                "job": job_path, "build": build_number,
                "error": str(exc)[:100],
            }

        lines = text.splitlines()
        match_indices = [i for i, line in enumerate(lines) if compiled.search(line)]

        if not match_indices:
            return {"job": job_path, "build": build_number, "matches": 0}

        with lock:
            state["match_count"] += len(match_indices)
            if state["match_count"] >= _SEARCH_ACROSS_TOTAL_MATCHES:
                stop_event.set()

        match_set = set(match_indices)
        capped = match_indices[:_SEARCH_ACROSS_MAX_MATCHES_PER_JOB]
        ranges: list[tuple[int, int]] = []
        for idx in capped:
            s = max(0, idx - context_lines)
            e = min(len(lines) - 1, idx + context_lines)
            if ranges and s <= ranges[-1][1] + 1:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], e))
            else:
                ranges.append((s, e))

        snippets: list[str] = []
        for s, e in ranges:
            snippet = []
            for i in range(s, e + 1):
                marker = ">>>" if i in match_set else "   "
                snippet.append(f"{marker} L{i + 1}: {lines[i]}")
            snippets.append("\n".join(snippet))

        return {
            "job": job_path, "build": build_number,
            "result": build_result,
            "matches": len(match_indices),
            "snippets": snippets,
        }

    search_results: list[dict] = []
    errors: list[dict] = []
    no_match_count = 0
    skipped_count = 0

    with ThreadPoolExecutor(max_workers=_SEARCH_ACROSS_WORKERS) as executor:
        futures = {
            executor.submit(_search_one, *c): c for c in candidates
        }
        for future in as_completed(futures):
            r = future.result()
            if r.get("skipped"):
                skipped_count += 1
            elif r.get("error"):
                errors.append(r)
            elif r.get("matches", 0) == 0:
                no_match_count += 1
            else:
                search_results.append(r)

    search_results.sort(key=lambda r: r.get("matches", 0), reverse=True)

    # --- Step 3: Format output ---
    sections: list[str] = []
    matched_jobs = len(search_results)
    total_hits = sum(r.get("matches", 0) for r in search_results)

    sections.append(
        f"=== SEARCH RESULTS: \"{pattern}\" across {folder or '(root)'} ===\n"
        f"Searched: {len(candidates)} jobs | "
        f"Matched: {matched_jobs} jobs | "
        f"Total hits: {total_hits}"
    )

    for r in search_results:
        header = (
            f"\n--- {r['job']} #{r['build']} ({r.get('result', '?')}) "
            f"— {r['matches']} match{'es' if r['matches'] != 1 else ''} ---"
        )
        sections.append(header)
        for snippet in r.get("snippets", []):
            sections.append(snippet)

    footer_parts = []
    if no_match_count:
        footer_parts.append(f"{no_match_count} jobs had no matches")
    if skipped_count:
        footer_parts.append(f"{skipped_count} skipped (match limit reached)")
    if errors:
        footer_parts.append(f"{len(errors)} jobs had errors")
    capped = total_jobs_found - len(candidates)
    if capped > 0:
        footer_parts.append(f"{capped} jobs not searched (cap: {_SEARCH_ACROSS_MAX_JOBS})")

    if footer_parts:
        sections.append(f"\n[{' | '.join(footer_parts)}]")

    if subfolders and not recursive:
        sections.append(
            f"{len(subfolders)} subfolders not searched (use recursive=true)"
        )

    time.sleep(TOOL_DELAY)

    result = "\n".join(sections)
    result_lines = result.splitlines()
    if len(result_lines) > _SEARCH_ACROSS_OUTPUT_CAP:
        result = "\n".join(result_lines[:_SEARCH_ACROSS_OUTPUT_CAP])
        result += f"\n[Output truncated at {_SEARCH_ACROSS_OUTPUT_CAP} lines]"
    return result


_TRIAGE_HARD_CAP = 200
_TRIAGE_MAX_ENRICH = 10


@mcp.tool
def triage_folder(folder: str, recursive: bool = False) -> str:
    """Scan a Jenkins folder for failing jobs: summary counts, failing job list
    with consecutive-failure count, and subfolder hints.

    Args:
        folder: Folder path to triage.
        recursive: If true, recurse into subfolders (default false).
    """
    try:
        jobs = jenkins_api.get_folder_jobs(folder)
    except Exception as exc:
        time.sleep(TOOL_DELAY)
        return _handle_error(exc, "triage_folder")

    real_jobs = [j for j in jobs if j.get("_class", "") not in _FOLDER_CLASSES]
    subfolders = [j for j in jobs if j.get("_class", "") in _FOLDER_CLASSES]

    # Recurse if requested
    if recursive and subfolders:
        for sf in subfolders[:10]:
            time.sleep(_BUNDLE_PACING)
            try:
                sub_path = f"{folder}/{sf['name']}" if folder else sf["name"]
                sub_jobs = jenkins_api.get_folder_jobs(sub_path)
                for sj in sub_jobs:
                    if sj.get("_class", "") not in _FOLDER_CLASSES:
                        sj["name"] = f"{sf['name']}/{sj['name']}"
                        real_jobs.append(sj)
            except Exception:
                pass

    failing = [j for j in real_jobs if j.get("color", "").startswith("red")]
    unstable = [j for j in real_jobs if j.get("color", "").startswith("yellow")]
    healthy = [j for j in real_jobs if j.get("color", "").startswith("blue")]
    other = [j for j in real_jobs if j not in failing and j not in unstable and j not in healthy]

    sections: list[str] = []
    sections.append(
        f"=== FOLDER TRIAGE: {folder or '(root)'} ===\n"
        f"Total: {len(real_jobs)} jobs — "
        f"{len(healthy)} healthy, {len(failing)} failing, {len(unstable)} unstable, {len(other)} other"
    )

    # Enrich top failing jobs with history
    if failing:
        fail_lines = [f"\n=== FAILING JOBS ({len(failing)}) ==="]

        enriched = 0
        for j in sorted(failing, key=lambda x: x.get("last_timestamp") or 0, reverse=True):
            consecutive = "?"
            if enriched < _TRIAGE_MAX_ENRICH:
                time.sleep(_BUNDLE_PACING)
                try:
                    h = jenkins_api.get_build_history(
                        f"{folder}/{j['name']}" if folder else j["name"], 5,
                    )
                    consecutive = 0
                    for b in h:
                        if b["result"] in ("FAILURE", "ABORTED"):
                            consecutive += 1
                        else:
                            break
                    consecutive = str(consecutive)
                except Exception:
                    pass
                enriched += 1

            bn = j.get("last_build_number", "?")
            ts = j.get("last_timestamp")
            age = ""
            if ts:
                age_s = (time.time() - ts / 1000)
                if age_s < 3600:
                    age = f"{int(age_s / 60)}m ago"
                elif age_s < 86400:
                    age = f"{int(age_s / 3600)}h ago"
                else:
                    age = f"{int(age_s / 86400)}d ago"

            fail_lines.append(f"  {j['name']:<40} #{bn:<8} {age:<10} ({consecutive} consecutive)")

        sections.append("\n".join(fail_lines))

    if unstable:
        unstable_lines = [f"\n=== UNSTABLE JOBS ({len(unstable)}) ==="]
        for j in unstable[:10]:
            bn = j.get("last_build_number", "?")
            unstable_lines.append(f"  {j['name']:<40} #{bn}")
        if len(unstable) > 10:
            unstable_lines.append(f"  ... and {len(unstable) - 10} more")
        sections.append("\n".join(unstable_lines))

    if subfolders and not recursive:
        sections.append(
            f"\n{len(subfolders)} subfolders not scanned (use recursive=true to include them): "
            + ", ".join(sf["name"] for sf in subfolders[:10])
        )

    time.sleep(TOOL_DELAY)

    result = "\n".join(sections)
    result_lines = result.splitlines()
    if len(result_lines) > _TRIAGE_HARD_CAP:
        result = "\n".join(result_lines[:_TRIAGE_HARD_CAP])
        result += f"\n[Output truncated at {_TRIAGE_HARD_CAP} lines]"
    return result


def main() -> None:
    import socket

    transport = os.getenv("MCP_TRANSPORT", "http")
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))

    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as _s:
            try:
                _s.connect(("8.8.8.8", 80))
                external_ip = _s.getsockname()[0]
            except OSError:
                external_ip = "127.0.0.1"

        print(
            f"Jenkins Investigator MCP server starting\n"
            f"  Local:    http://127.0.0.1:{port}/mcp\n"
            f"  Network:  http://{external_ip}:{port}/mcp",
            file=sys.stderr,
        )
        mcp.run(transport=transport, host=host, port=port, show_banner=False)


if __name__ == "__main__":
    main()
