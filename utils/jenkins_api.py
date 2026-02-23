"""
Clean wrappers for Jenkins REST API calls.

All functions raise meaningful exceptions rather than returning error strings,
so callers (MCP tools) can decide how to surface the failure.
"""

import logging
import os
import re
import time
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_JENKINS_URL = os.environ.get("JENKINS_URL", "").rstrip("/")
_JENKINS_USER = os.environ.get("JENKINS_USER", "")
_JENKINS_TOKEN = os.environ.get("JENKINS_TOKEN", "")

_MISSING = [k for k, v in {
    "JENKINS_URL": _JENKINS_URL,
    "JENKINS_USER": _JENKINS_USER,
    "JENKINS_TOKEN": _JENKINS_TOKEN,
}.items() if not v]

if _MISSING:
    raise EnvironmentError(
        f"Missing required environment variables: {', '.join(_MISSING)}. "
        "Copy .env.example to .env and fill in your credentials."
    )

_AUTH = (_JENKINS_USER, _JENKINS_TOKEN)
_TIMEOUT = 30

_VERIFY_SSL = os.environ.get("JENKINS_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

if not _VERIFY_SSL:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_MAX_LOG_BYTES = 10 * 1024 * 1024   # 10 MB
_MAX_ARTIFACT_BYTES = 50 * 1024     # 50 KB — artifact content goes into context window
_MAX_CONFIG_BYTES = 50 * 1024       # 50 KB
_MAX_QUEUE_ITEMS = 50
_MAX_FOLDER_JOBS = 100

_BUILD_KEEP_FIELDS = {"result", "duration", "timestamp", "builtOn", "changeSet", "changeSets", "actions"}
_FAILING_STATUSES = {"FAILED", "REGRESSION"}

_RETRYABLE_STATUSES = {429, 502, 503, 504}
_MAX_RETRIES = 2
_RETRY_DELAYS = (1, 3)  # seconds between retry 0→1 and 1→2

_HTML_TAG_RE = re.compile(r"<[^>]+>")

_TEXT_EXTENSIONS = frozenset({
    ".txt", ".log", ".json", ".xml", ".yaml", ".yml", ".html", ".csv",
    ".properties", ".groovy", ".sh", ".py", ".out", ".cfg", ".ini", ".md",
    ".toml", ".conf", ".env", ".tf", ".hcl", ".rb", ".go", ".java",
})
_BINARY_EXTENSIONS = frozenset({
    ".jar", ".war", ".zip", ".gz", ".tar", ".bz2", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".pdf", ".class", ".exe", ".dll", ".so", ".dylib",
    ".whl", ".egg", ".deb", ".rpm",
})


def _get(path: str, **kwargs) -> requests.Response:
    """HTTP GET with bounded retry for transient failures (429/502/503/504)."""
    url = f"{_JENKINS_URL}{path}"
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = requests.get(
                url, auth=_AUTH, timeout=_TIMEOUT, verify=_VERIFY_SSL, **kwargs,
            )
            if response.status_code in _RETRYABLE_STATUSES and attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            logger.debug("Jenkins HTTP %s for %s", exc.response.status_code, url)
            raise
        except requests.ConnectionError:
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise ConnectionError(
                f"Cannot reach Jenkins at {_JENKINS_URL}. "
                "Verify the server is running and JENKINS_URL is correct."
            )
        except requests.Timeout:
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise TimeoutError(
                f"Jenkins did not respond within {_TIMEOUT} seconds ({url})."
            )
    raise RuntimeError(f"Exhausted retries for {url}")


def _job_path(job_name: str) -> str:
    """Convert a slash-separated job name into a Jenkins API path segment.

    Each segment is URL-encoded to handle spaces, '#', '%', etc.
    'my-org/my-repo/main' -> '/job/my-org/job/my-repo/job/main'
    'simple-job'          -> '/job/simple-job'
    """
    segments = [quote(seg, safe="") for seg in job_name.split("/")]
    return "/job/" + "/job/".join(segments)


def _strip_html(html: str) -> str:
    """Remove HTML tags from wfapi log responses."""
    return _HTML_TAG_RE.sub("", html)


_BUILD_SELECTOR_MAP = {
    "last":        "lastBuild",
    "last_failed": "lastFailedBuild",
    "last_stable": "lastStableBuild",
    "last_success": "lastSuccessfulBuild",
}


def get_named_build(job_name: str, selector: str = "last_failed") -> dict:
    """
    Fetch a named build (last, last_failed, last_stable, last_success) for a job.

    Returns a pruned dict including the build number so callers can chain into
    the other build-specific functions without knowing the build number upfront.
    """
    endpoint = _BUILD_SELECTOR_MAP.get(selector, "lastFailedBuild")
    path = f"{_job_path(job_name)}/{endpoint}/api/json"
    try:
        data = _get(path).json()
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            return {}
        raise
    keep = _BUILD_KEEP_FIELDS | {"number", "url"}
    return {k: v for k, v in data.items() if k in keep}


def get_build(job_name: str, build_number: int) -> dict:
    """
    Fetch build metadata and prune it to only the fields the AI needs.

    Returns a dict with keys: result, duration, timestamp, builtOn,
    changeSet/changeSets (whichever exists), and actions.
    """
    path = f"{_job_path(job_name)}/{build_number}/api/json"
    data = _get(path).json()
    return {k: v for k, v in data.items() if k in _BUILD_KEEP_FIELDS}


def get_build_trigger(actions: list) -> str:
    """
    Walk the build's actions array to extract a human-readable trigger description.

    Jenkins stores trigger metadata inside actions[].causes[].
    The cause shape differs by trigger type (user, timer, upstream, SCM poll).
    """
    for action in actions:
        causes = action.get("causes")
        if not causes:
            continue
        for cause in causes:
            if "userId" in cause:
                user = cause.get("userName", cause["userId"])
                return f"Triggered by user: {user} ({cause['userId']})"
            if "upstreamProject" in cause:
                proj = cause["upstreamProject"]
                build = cause.get("upstreamBuild", "?")
                return f"Triggered by upstream job: {proj} #{build}"
            desc = cause.get("shortDescription")
            if desc:
                return desc
    return "Unknown trigger"


def get_console_text(job_name: str, build_number: int) -> str:
    """Fetch the console log for a build, streaming and capping at 10 MB."""
    path = f"{_job_path(job_name)}/{build_number}/consoleText"
    response = _get(path, stream=True)
    chunks: list[str] = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
        total += len(chunk)
        chunks.append(chunk)
        if total >= _MAX_LOG_BYTES:
            chunks.append("\n[LOG TRUNCATED: exceeded 10 MB download limit]")
            break
    response.close()
    return "".join(chunks)


_TAIL_PROBE_OFFSET = 2_147_483_647


def get_console_text_tail(job_name: str, build_number: int,
                          max_bytes: int = 500_000) -> str:
    """Fetch only the tail of a console log to save bandwidth.

    Probes log size via Jenkins progressiveText (a request beyond the end
    returns an empty body but includes X-Text-Size in the response header).
    If the log is smaller than max_bytes, falls back to get_console_text.
    """
    probe_path = (
        f"{_job_path(job_name)}/{build_number}"
        f"/logText/progressiveText?start={_TAIL_PROBE_OFFSET}"
    )
    try:
        probe = _get(probe_path)
        log_size = int(probe.headers.get("X-Text-Size", 0))
    except Exception:
        return get_console_text(job_name, build_number)

    if log_size <= max_bytes:
        return get_console_text(job_name, build_number)

    start = log_size - max_bytes
    tail_path = (
        f"{_job_path(job_name)}/{build_number}"
        f"/logText/progressiveText?start={start}"
    )
    try:
        resp = _get(tail_path)
        resp.encoding = "utf-8"
        return resp.text
    except Exception:
        return get_console_text(job_name, build_number)


def get_test_report(job_name: str, build_number: int) -> dict | None:
    """
    Fetch the JUnit/TestNG test report for a build.

    Returns None (not an error) when no report exists (HTTP 404).
    Returns a pruned dict with overall counts and only the failing test cases.
    """
    path = f"{_job_path(job_name)}/{build_number}/testReport/api/json"
    try:
        data = _get(path).json()
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            return None
        raise

    failing_cases = []
    for suite in data.get("suites") or []:
        for case in suite.get("cases") or []:
            if case.get("status") in _FAILING_STATUSES:
                failing_cases.append({
                    "class_name": case.get("className", ""),
                    "test_name": case.get("name", ""),
                    "error_details": (case.get("errorDetails") or "")[:500],
                    "error_stack_trace": (case.get("errorStackTrace") or "")[:1000],
                })

    return {
        "fail_count": data.get("failCount", 0),
        "pass_count": data.get("passCount", 0),
        "skip_count": data.get("skipCount", 0),
        "failing_tests": failing_cases,
    }


def get_pipeline_stages(job_name: str, build_number: int) -> list[dict] | None:
    """Fetch structured stage data for a Pipeline build via the Workflow API.

    Returns a list of dicts with keys: name, status, duration_s.
    Returns None when the job is not a Pipeline (wfapi returns 404).
    """
    path = f"{_job_path(job_name)}/{build_number}/wfapi/describe"
    try:
        data = _get(path).json()
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            return None
        raise

    stages = []
    for stage in data.get("stages") or []:
        stages.append({
            "id": stage.get("id"),
            "name": stage.get("name", ""),
            "status": stage.get("status", "UNKNOWN"),
            "duration_s": round((stage.get("durationMillis") or 0) / 1000, 1),
        })
    return stages


def extract_parameters(actions: list) -> list[dict]:
    """Extract build parameters from the actions array.

    Jenkins stores parameters inside an action with
    _class == "hudson.model.ParametersAction".  Values longer than 200
    characters are truncated to keep the output concise.
    """
    for action in actions:
        if action.get("_class") == "hudson.model.ParametersAction":
            params = action.get("parameters") or []
            result = []
            for p in params:
                name = p.get("name", "")
                value = str(p.get("value", ""))
                if len(value) > 200:
                    value = value[:200] + "…[truncated]"
                result.append({"name": name, "value": value})
            return result
    return []


_MAX_HISTORY = 25


def get_build_history(job_name: str, count: int = 10) -> list[dict]:
    """Fetch the last N builds for a job.

    Includes builtOn (agent) to enable per-node failure correlation in
    bundles without needing N+1 per-build fetches.
    Count is clamped to _MAX_HISTORY (25) to keep output small.
    """
    count = max(1, min(count, _MAX_HISTORY))
    tree = f"builds[number,result,duration,timestamp,builtOn]{{0,{count}}}"
    path = f"{_job_path(job_name)}/api/json?tree={tree}"
    data = _get(path).json()

    builds = []
    for b in data.get("builds") or []:
        builds.append({
            "number": b.get("number"),
            "result": b.get("result") or "IN_PROGRESS",
            "duration_s": round((b.get("duration") or 0) / 1000, 1),
            "timestamp": b.get("timestamp"),
            "agent": b.get("builtOn") or "controller",
        })
    return builds


# ---------------------------------------------------------------------------
# Stage / flow-node APIs (Workflow API)
# ---------------------------------------------------------------------------


def get_stage_log(job_name: str, build_number: int, node_id: str) -> str:
    """Fetch the console log for a single pipeline stage, with pagination.

    The wfapi log endpoint returns HTML-wrapped text and paginates via
    ``hasMore`` / ``length``.  We loop until the full log is collected
    (or _MAX_LOG_BYTES is reached), then strip HTML tags.
    """
    chunks: list[str] = []
    total = 0
    start = 0

    while True:
        path = f"{_job_path(job_name)}/{build_number}/execution/node/{node_id}/wfapi/log"
        if start:
            path += f"?start={start}"
        try:
            data = _get(path).json()
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                return ""
            raise

        text = _strip_html(data.get("text", ""))
        chunks.append(text)
        total += len(text)

        if total >= _MAX_LOG_BYTES:
            chunks.append("\n[STAGE LOG TRUNCATED: exceeded 10 MB limit]")
            break
        if not data.get("hasMore", False):
            break

        new_start = data.get("length", 0)
        if new_start <= start:
            break
        start = new_start

    return "".join(chunks)


def get_flow_node_detail(
    job_name: str, build_number: int, node_id: str,
) -> dict | None:
    """Fetch child flow-node details for a pipeline stage (parallel branches)."""
    path = f"{_job_path(job_name)}/{build_number}/execution/node/{node_id}/wfapi/describe"
    try:
        data = _get(path).json()
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            return None
        raise

    flow_nodes = []
    for fn in data.get("stageFlowNodes") or []:
        flow_nodes.append({
            "id": fn.get("id", ""),
            "name": fn.get("name", ""),
            "status": fn.get("status", "UNKNOWN"),
            "duration_s": round((fn.get("durationMillis") or 0) / 1000, 1),
        })

    return {
        "id": data.get("id", ""),
        "name": data.get("name", ""),
        "status": data.get("status", "UNKNOWN"),
        "duration_s": round((data.get("durationMillis") or 0) / 1000, 1),
        "stage_flow_nodes": flow_nodes,
    }


# ---------------------------------------------------------------------------
# Artifact APIs
# ---------------------------------------------------------------------------


def get_artifacts_list(job_name: str, build_number: int) -> list[dict]:
    """List build artifacts (metadata only, no content)."""
    path = f"{_job_path(job_name)}/{build_number}/api/json?tree=artifacts[relativePath,fileName]"
    data = _get(path).json()
    return [
        {"relative_path": a.get("relativePath", ""), "file_name": a.get("fileName", "")}
        for a in (data.get("artifacts") or [])
    ]


def get_artifact_content(
    job_name: str,
    build_number: int,
    artifact_path: str,
    max_bytes: int = _MAX_ARTIFACT_BYTES,
) -> str | None:
    """Fetch a text artifact's content, returning None for binary files.

    Detection: known binary extensions are rejected upfront.  Unknown
    extensions are probed via a UTF-8 decode of the first 8 KB.
    """
    ext = os.path.splitext(artifact_path)[1].lower()
    if ext in _BINARY_EXTENSIONS:
        return None

    encoded_path = "/".join(quote(seg, safe="") for seg in artifact_path.split("/"))
    path = f"{_job_path(job_name)}/{build_number}/artifact/{encoded_path}"
    response = _get(path, stream=True)

    raw_chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192):
        total += len(chunk)
        raw_chunks.append(chunk)
        if total >= max_bytes:
            break
    response.close()

    raw = b"".join(raw_chunks)

    if ext not in _TEXT_EXTENSIONS:
        try:
            raw[:8192].decode("utf-8")
        except UnicodeDecodeError:
            return None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None

    if total >= max_bytes:
        text += f"\n[ARTIFACT TRUNCATED: exceeded {max_bytes // 1024} KB limit]"
    return text


# ---------------------------------------------------------------------------
# Job configuration
# ---------------------------------------------------------------------------


def get_job_config_xml(job_name: str, max_bytes: int = _MAX_CONFIG_BYTES) -> str:
    """Fetch the raw config.xml for a job, capped to max_bytes."""
    path = f"{_job_path(job_name)}/config.xml"
    response = _get(path, stream=True)
    chunks: list[str] = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192, decode_unicode=True):
        total += len(chunk)
        chunks.append(chunk)
        if total >= max_bytes:
            chunks.append("\n<!-- CONFIG TRUNCATED -->")
            break
    response.close()
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Queue / folder / node discovery
# ---------------------------------------------------------------------------


def get_queue(job_filter: str = "") -> list[dict]:
    """Fetch the Jenkins build queue, optionally filtered by job name."""
    tree = "items[id,task[name],why,blocked,stuck,inQueueSince,buildableStartMilliseconds]"
    path = f"/queue/api/json?tree={tree}"
    data = _get(path).json()

    items = []
    for item in (data.get("items") or [])[:_MAX_QUEUE_ITEMS]:
        task_name = (item.get("task") or {}).get("name", "")
        if job_filter and job_filter.lower() not in task_name.lower():
            continue
        items.append({
            "id": item.get("id"),
            "task_name": task_name,
            "why": item.get("why", ""),
            "blocked": item.get("blocked", False),
            "stuck": item.get("stuck", False),
            "in_queue_since_ms": item.get("inQueueSince") or 0,
        })
    return items


def get_folder_jobs(folder: str = "", include_last_failed: bool = False) -> list[dict]:
    """List jobs in a Jenkins folder (or root) with last-build status.

    Returns up to _MAX_FOLDER_JOBS items.  Includes ``_class`` so callers
    can distinguish real jobs from sub-folders.

    When *include_last_failed* is True the tree query also fetches
    ``lastFailedBuild[number]`` and each returned dict contains an
    extra ``last_failed_build_number`` key.
    """
    tree = "jobs[name,color,url,_class,lastBuild[number,result,timestamp]"
    if include_last_failed:
        tree += ",lastFailedBuild[number]"
    tree += "]"

    if folder:
        path = f"{_job_path(folder)}/api/json?tree={tree}"
    else:
        path = f"/api/json?tree={tree}"
    data = _get(path).json()

    all_jobs = data.get("jobs") or []
    jobs = []
    for j in all_jobs[:_MAX_FOLDER_JOBS]:
        last = j.get("lastBuild") or {}
        entry = {
            "name": j.get("name", ""),
            "color": j.get("color", ""),
            "url": j.get("url", ""),
            "_class": j.get("_class", ""),
            "last_build_number": last.get("number"),
            "last_result": last.get("result"),
            "last_timestamp": last.get("timestamp"),
        }
        if include_last_failed:
            last_failed = j.get("lastFailedBuild") or {}
            entry["last_failed_build_number"] = last_failed.get("number")
        jobs.append(entry)

    if len(all_jobs) > _MAX_FOLDER_JOBS:
        logger.debug("Folder %s has >%d jobs; list truncated", folder or "(root)", _MAX_FOLDER_JOBS)
    return jobs


def get_all_nodes() -> list[dict]:
    """List all Jenkins agents with labels, executor counts, and disk space."""
    tree = (
        "computer[displayName,offline,offlineCauseReason,"
        "assignedLabels[name],numExecutors,idle,"
        "monitorData[hudson.node_monitors.DiskSpaceMonitor]]"
    )
    path = f"/computer/api/json?tree={tree}"
    data = _get(path).json()

    nodes = []
    for c in data.get("computer") or []:
        disk_gb: float | None = None
        disk_monitor = (c.get("monitorData") or {}).get(
            "hudson.node_monitors.DiskSpaceMonitor"
        )
        if isinstance(disk_monitor, dict):
            size_bytes = disk_monitor.get("size")
            if size_bytes is not None:
                disk_gb = round(size_bytes / (1024 ** 3), 2)

        labels = [
            lbl.get("name", "")
            for lbl in (c.get("assignedLabels") or [])
            if lbl.get("name")
        ]

        nodes.append({
            "name": c.get("displayName", ""),
            "online": not c.get("offline", True),
            "offline_reason": c.get("offlineCauseReason") or None,
            "labels": labels,
            "executors": c.get("numExecutors", 0),
            "idle": c.get("idle", False),
            "disk_gb": disk_gb,
        })
    return nodes


# ---------------------------------------------------------------------------
# Environment variables (requires EnvInject plugin)
# ---------------------------------------------------------------------------


def get_injected_env_vars(job_name: str, build_number: int) -> dict | None:
    """Fetch injected environment variables.  Returns None if the EnvInject
    plugin is not installed (404)."""
    path = f"{_job_path(job_name)}/{build_number}/injectedEnvVars/api/json"
    try:
        data = _get(path).json()
    except requests.HTTPError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return data.get("envMap") or {}


# ---------------------------------------------------------------------------
# Node status
# ---------------------------------------------------------------------------


def get_node_info(node_name: str) -> dict:
    """
    Fetch status and disk metrics for a Jenkins build agent.

    The special name 'master' or 'built-in' maps to the Jenkins controller node.
    Disk space is parsed from monitorData and converted from bytes to GB.
    """
    encoded_name = "(master)" if node_name.lower() in ("master", "built-in") else node_name
    path = f"/computer/{encoded_name}/api/json"
    data = _get(path).json()

    disk_gb: float | None = None
    disk_monitor = (data.get("monitorData") or {}).get(
        "hudson.node_monitors.DiskSpaceMonitor"
    )
    if isinstance(disk_monitor, dict):
        size_bytes = disk_monitor.get("size")
        if size_bytes is not None:
            disk_gb = round(size_bytes / (1024 ** 3), 2)

    return {
        "display_name": data.get("displayName", node_name),
        "online": not data.get("offline", True),
        "offline_reason": (data.get("offlineCauseReason") or None),
        "disk_space_gb": disk_gb,
        "disk_warning": (disk_gb is not None and disk_gb < 10),
    }
