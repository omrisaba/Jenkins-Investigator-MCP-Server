"""
Pure JUnit/xUnit XML parser — no API calls, no formatting, no budgeting.

Accepts raw XML content and returns structured Python dicts.  The caller
(server.py) is responsible for token budgets, provenance, and formatting.

Supported formats:
  - Standard JUnit XML (<testsuite> or <testsuites> root)
  - Maven Surefire, Gradle, pytest --junitxml, Go go-junit-report
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_MAX_DETAIL_CHARS = 5000
_MAX_OUTPUT_CHARS = 2000

_ASSERTION_TYPE_RE = re.compile(
    r'Assertion|ComparisonFailure|ExpectationFailure',
    re.IGNORECASE,
)
_EXCEPTION_TYPE_RE = re.compile(
    r'Exception|Error|Timeout|Refused',
    re.IGNORECASE,
)
_EXCEPTION_MSG_RE = re.compile(
    r'\w+Exception\b|\w+Error\b|Timeout\b|Refused\b',
    re.IGNORECASE,
)


def parse_junit_xml(xml_content: str) -> list[dict] | None:
    """Parse JUnit XML into a list of suite dicts.

    Returns None if the content is not valid JUnit XML (wrong root element
    or malformed XML).  Returns an empty list for valid JUnit with no suites.

    Each suite dict contains:
      suite_name, suite_time_s, suite_tests, suite_failures, suite_errors,
      suite_skipped, suite_stderr, suite_stdout, cases (list of case dicts)

    Each case dict contains:
      name, class_name, time_s, status, kind, message, detail,
      stdout, stderr, skip_reason
    """
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError:
        return None

    if root.tag == "testsuites":
        suite_elements = list(root)
    elif root.tag == "testsuite":
        suite_elements = [root]
    else:
        return None

    suites: list[dict] = []
    for suite_el in suite_elements:
        if suite_el.tag != "testsuite":
            continue
        suites.append(_parse_suite(suite_el))

    return suites


def _parse_suite(suite_el: ET.Element) -> dict:
    cases: list[dict] = []
    for case_el in suite_el.findall("testcase"):
        cases.append(_parse_case(case_el))

    return {
        "suite_name": suite_el.get("name", ""),
        "suite_time_s": _safe_float(suite_el.get("time")),
        "suite_tests": _safe_int(suite_el.get("tests")),
        "suite_failures": _safe_int(suite_el.get("failures")),
        "suite_errors": _safe_int(suite_el.get("errors")),
        "suite_skipped": _safe_int(suite_el.get("skipped")),
        "suite_stdout": _element_text(suite_el, "system-out", _MAX_OUTPUT_CHARS),
        "suite_stderr": _element_text(suite_el, "system-err", _MAX_OUTPUT_CHARS),
        "cases": cases,
    }


def _parse_case(case_el: ET.Element) -> dict:
    failure_el = case_el.find("failure")
    error_el = case_el.find("error")
    skipped_el = case_el.find("skipped")

    if failure_el is not None:
        status = "failed"
        message = failure_el.get("message", "")
        detail = (failure_el.text or "")[:_MAX_DETAIL_CHARS]
        kind = _infer_kind(failure_el.get("type", ""), message, "failure")
    elif error_el is not None:
        status = "errored"
        message = error_el.get("message", "")
        detail = (error_el.text or "")[:_MAX_DETAIL_CHARS]
        kind = _infer_kind(error_el.get("type", ""), message, "error")
    elif skipped_el is not None:
        status = "skipped"
        kind = "skipped"
        message = skipped_el.get("message", "")
        detail = ""
    else:
        status = "passed"
        kind = "passed"
        message = ""
        detail = ""

    return {
        "name": case_el.get("name", ""),
        "class_name": case_el.get("classname", ""),
        "time_s": _safe_float(case_el.get("time")),
        "status": status,
        "kind": kind,
        "message": message,
        "detail": detail,
        "stdout": _element_text(case_el, "system-out", _MAX_OUTPUT_CHARS),
        "stderr": _element_text(case_el, "system-err", _MAX_OUTPUT_CHARS),
        "skip_reason": message if status == "skipped" else "",
    }


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def classify_failures(suites: list[dict]) -> dict:
    """Count assertion failures vs exception errors across all suites.

    Returns:
        {"assertions": int, "exceptions": int, "total_failed": int}
    """
    assertions = 0
    exceptions = 0
    for suite in suites:
        for case in suite["cases"]:
            if case["kind"] == "assertion":
                assertions += 1
            elif case["kind"] == "exception":
                exceptions += 1
    return {
        "assertions": assertions,
        "exceptions": exceptions,
        "total_failed": assertions + exceptions,
    }


def detect_blast_radius(suites: list[dict], threshold: float = 0.6) -> list[dict]:
    """Find suites where many failures share the same root error.

    A blast radius is detected when a suite has >= 3 failures and >= threshold
    fraction share the same error message (first 100 chars).

    Returns a list of blast-radius dicts:
      suite_name, total_failures, shared_count, shared_message, suite_stderr,
      affected_tests (list of test names)
    """
    blasts: list[dict] = []

    for suite in suites:
        failing = [c for c in suite["cases"] if c["status"] in ("failed", "errored")]
        if len(failing) < 3:
            continue

        buckets: dict[str, list[dict]] = {}
        for case in failing:
            key = case["message"][:100] or case["detail"][:100] or "(no message)"
            buckets.setdefault(key, []).append(case)

        for msg_key, cases in buckets.items():
            if len(cases) >= 3 and len(cases) / len(failing) >= threshold:
                blasts.append({
                    "suite_name": suite["suite_name"],
                    "total_failures": len(failing),
                    "shared_count": len(cases),
                    "shared_message": msg_key,
                    "suite_stderr": suite["suite_stderr"],
                    "affected_tests": [c["name"] for c in cases],
                    "affected_qualified_names": [
                        f"{c['class_name']}.{c['name']}" if c["class_name"] else c["name"]
                        for c in cases
                    ],
                })

    return blasts


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _infer_kind(type_attr: str, message: str, element_tag: str) -> str:
    """Classify a test failure as 'assertion' or 'exception'.

    Priority: type attribute > message content > element tag.
    The type attribute (e.g. 'junit.framework.AssertionError') is the
    strongest signal.  Frameworks like pytest put all failures under
    <failure> regardless of cause, so the element tag alone is unreliable.
    """
    if type_attr:
        if _ASSERTION_TYPE_RE.search(type_attr):
            return "assertion"
        if _EXCEPTION_TYPE_RE.search(type_attr):
            return "exception"

    combined = f"{type_attr} {message}"
    if _ASSERTION_TYPE_RE.search(combined):
        return "assertion"
    if _EXCEPTION_MSG_RE.search(combined):
        return "exception"

    return "assertion" if element_tag == "failure" else "exception"


def _element_text(parent: ET.Element, tag: str, max_chars: int) -> str:
    el = parent.find(tag)
    if el is None or not el.text:
        return ""
    return el.text[:max_chars]


def _safe_float(val: str | None) -> float:
    if val is None:
        return 0.0
    try:
        return round(float(val), 3)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(val: str | None) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
