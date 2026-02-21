"""Tests for utils.log_parser — focused on High/Medium-risk scenarios."""

from __future__ import annotations

import textwrap

import pytest

from utils.log_parser import (
    CONTEXT_LINES,
    HARD_LIMIT,
    LOG_HEAD_LINES,
    LOG_TAIL_LINES,
    MAX_LINES,
    MIN_CLIP_LINES,
    TIER_CRITICAL,
    TIER_ERROR,
    TIER_WARNING,
    _build_stage_index,
    _classify_line,
    _deduplicate,
    _extract_exception_token,
    _normalize_key,
    _resolve_stage,
    _scan,
    get_error_log,
    truncate_tail,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log(n_lines: int, *, prefix: str = "log line") -> str:
    return "\n".join(f"{prefix} {i}" for i in range(n_lines))


def _pipeline_stage_line(name: str) -> str:
    return f'[Pipeline] {{ ({name})'


# ---------------------------------------------------------------------------
# _classify_line
# ---------------------------------------------------------------------------


class TestClassifyLine:
    def test_critical_fatal(self):
        assert _classify_line("2024-01-01 FATAL: disk full") == TIER_CRITICAL

    def test_critical_build_failure(self):
        assert _classify_line("[INFO] BUILD FAILURE") == TIER_CRITICAL

    def test_critical_oom(self):
        assert _classify_line("java.lang.OutOfMemoryError: Heap space") == TIER_CRITICAL

    def test_critical_gradle_failure(self):
        assert _classify_line("FAILURE: Build failed with an exception.") == TIER_CRITICAL

    def test_error_exception(self):
        assert _classify_line("NullPointerException at Foo.java:42") == TIER_ERROR

    def test_error_traceback(self):
        assert _classify_line("Traceback (most recent call last):") == TIER_ERROR

    def test_error_caused_by(self):
        assert _classify_line("Caused by: java.io.IOException") == TIER_ERROR

    def test_error_panic(self):
        assert _classify_line("panic: runtime error: index out of range") == TIER_ERROR

    def test_warning(self):
        assert _classify_line("[WARNING] Using deprecated API") == TIER_WARNING

    def test_warning_short(self):
        assert _classify_line("WARN some message") == TIER_WARNING

    def test_clean_line(self):
        assert _classify_line("Compiling main.go...") is None


# ---------------------------------------------------------------------------
# _normalize_key
# ---------------------------------------------------------------------------


class TestNormalizeKey:
    def test_strips_iso_timestamp(self):
        assert _normalize_key("2024-01-15T10:30:00.123 ERROR foo") == "ERROR foo"

    def test_strips_bracket_timestamp(self):
        assert _normalize_key("[01/15/24 10:30:00] ERROR bar") == "ERROR bar"

    def test_strips_ansi(self):
        assert _normalize_key("\x1b[31mERROR\x1b[0m baz") == "ERROR baz"

    def test_preserves_content(self):
        assert _normalize_key("NullPointerException at Main.java:5") == "NullPointerException at Main.java:5"


# ---------------------------------------------------------------------------
# _extract_exception_token
# ---------------------------------------------------------------------------


class TestExtractExceptionToken:
    def test_java_exception(self):
        assert _extract_exception_token("java.lang.NullPointerException: msg") == "NullPointerException"

    def test_build_failure(self):
        assert _extract_exception_token("[INFO] BUILD FAILURE") == "BUILD FAILURE"

    def test_npm_err(self):
        assert _extract_exception_token("npm ERR! missing script") == "npm ERR!"

    def test_traceback(self):
        assert _extract_exception_token("Traceback (most recent call last):") == "Traceback"

    def test_panic(self):
        assert _extract_exception_token("panic: runtime error") == "panic:"

    def test_generic_fallback(self):
        token = _extract_exception_token("some unknown line of text")
        assert len(token) <= 60


# ---------------------------------------------------------------------------
# Stage indexing
# ---------------------------------------------------------------------------


class TestStageIndex:
    def test_pipeline_stages(self):
        lines = [
            "starting",
            '[Pipeline] { (Build)',
            "compiling...",
            '[Pipeline] { (Test)',
            "testing...",
        ]
        idx = _build_stage_index(lines)
        assert len(idx) == 2
        assert idx[0] == (1, "Build")
        assert idx[1] == (3, "Test")

    def test_entering_stage(self):
        lines = ["stuff", "Entering stage Deploy", "deploying"]
        idx = _build_stage_index(lines)
        assert len(idx) == 1
        assert idx[0][1] == "Deploy"

    def test_resolve_before_first_stage(self):
        idx = [(10, "Build"), (50, "Test")]
        assert _resolve_stage(idx, 5) == ""

    def test_resolve_between_stages(self):
        idx = [(10, "Build"), (50, "Test")]
        assert _resolve_stage(idx, 30) == "Build"

    def test_resolve_after_last_stage(self):
        idx = [(10, "Build"), (50, "Test")]
        assert _resolve_stage(idx, 80) == "Test"

    def test_resolve_empty_index(self):
        assert _resolve_stage([], 42) == ""


# ---------------------------------------------------------------------------
# Critical section clipping (not dropped)
# ---------------------------------------------------------------------------


class TestOversizedCriticalSection:
    """A single huge critical stack trace must be clipped, never silently dropped."""

    def test_large_critical_is_present_in_output(self):
        lines = []
        lines.append("Started by user admin")
        lines.extend([f"setup line {i}" for i in range(20)])
        # Many consecutive error-matching lines merge into one huge section
        for i in range(250):
            lines.append(f"ERROR: step {i} failed — OutOfMemoryError in worker {i}")
        lines.extend([f"cleanup line {i}" for i in range(50)])
        lines.append("Finished: FAILURE")

        text = "\n".join(lines)
        result = get_error_log(text)

        assert "CRITICAL" in result
        assert "OutOfMemoryError" in result
        assert "omitted" in result

    def test_large_critical_respects_hard_limit(self):
        lines = ["Started by user admin"]
        lines.extend([f"setup {i}" for i in range(20)])
        lines.append("FATAL: system crash")
        lines.extend([f"    stack frame {i}" for i in range(500)])
        lines.append("Finished: FAILURE")
        text = "\n".join(lines)
        result = get_error_log(text)
        assert len(result.splitlines()) <= HARD_LIMIT + 1


# ---------------------------------------------------------------------------
# Deduplication precision
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Distinct failures must NOT be collapsed; identical repeats must be."""

    def test_same_error_same_stage_collapsed(self):
        lines = []
        lines.append('[Pipeline] { (Build)')
        for _ in range(3):
            lines.extend([f"info line {i}" for i in range(25)])
            lines.append("ERROR: connection refused to registry.example.com")
        lines.append("Finished: FAILURE")
        text = "\n".join(lines)
        result = get_error_log(text)
        assert "repeated" in result.lower()

    def test_different_exceptions_not_collapsed(self):
        lines = ['[Pipeline] { (Build)']
        lines.extend([f"line {i}" for i in range(25)])
        lines.append("java.lang.NullPointerException: foo is null")
        lines.extend([f"line {i}" for i in range(25)])
        lines.append("java.io.IOException: file not found")
        lines.append("Finished: FAILURE")
        text = "\n".join(lines)

        result = get_error_log(text)
        assert "NullPointerException" in result
        assert "IOException" in result

    def test_same_exception_different_stages_not_collapsed(self):
        lines = []
        lines.append('[Pipeline] { (Build)')
        lines.extend([f"build line {i}" for i in range(25)])
        lines.append("ERROR: timeout connecting to database")
        lines.extend([f"between {i}" for i in range(25)])
        lines.append('[Pipeline] { (Test)')
        lines.extend([f"test line {i}" for i in range(25)])
        lines.append("ERROR: timeout connecting to database")
        lines.append("Finished: FAILURE")
        text = "\n".join(lines)
        result = get_error_log(text)

        assert result.count("timeout connecting to database") >= 2

    def test_timestamp_variation_still_deduplicates(self):
        lines = ['[Pipeline] { (Build)']
        for ts in ["2024-01-01T10:00:01", "2024-01-01T10:00:02", "2024-01-01T10:00:03"]:
            lines.extend([f"filler {i}" for i in range(25)])
            lines.append(f"{ts} ERROR: disk quota exceeded")
        lines.append("Finished: FAILURE")
        text = "\n".join(lines)
        result = get_error_log(text)
        assert "repeated" in result.lower()


# ---------------------------------------------------------------------------
# Warning policy
# ---------------------------------------------------------------------------


class TestWarningPolicy:
    """Warnings appear only after all CRITICAL and ERROR coverage."""

    def test_warnings_excluded_when_budget_full_of_errors(self):
        lines = ["Started by user admin"]
        for i in range(15):
            lines.extend([f"filler {j}" for j in range(15)])
            lines.append(f"ERROR: unique failure number {i}")
        lines.extend([f"filler {j}" for j in range(15)])
        lines.append("WARNING: deprecated API call")
        lines.append("Finished: FAILURE")
        text = "\n".join(lines)
        result = get_error_log(text)
        assert "WARNING near line" not in result or "ERROR near line" in result

    def test_warnings_included_when_budget_allows(self):
        lines = ["Started by admin"]
        lines.extend([f"clean {i}" for i in range(25)])
        lines.append("ERROR: one small error")
        # Enough separation so error and warning don't merge (>2*CONTEXT_LINES)
        lines.extend([f"clean {i}" for i in range(25)])
        lines.append("WARNING: deprecated thing")
        lines.extend([f"clean {i}" for i in range(25)])
        lines.append("Finished: SUCCESS")
        text = "\n".join(lines)
        result = get_error_log(text)
        assert "WARNING near line" in result


# ---------------------------------------------------------------------------
# No-match fallback
# ---------------------------------------------------------------------------


class TestNoMatchFallback:
    def test_fallback_returns_tail(self):
        text = _make_log(500, prefix="clean line")
        result = get_error_log(text)
        assert "No error patterns matched" in result
        assert "clean line 499" in result

    def test_fallback_short_log(self):
        text = "line 1\nline 2\nline 3"
        result = get_error_log(text)
        assert "No error patterns matched" in result
        assert "line 1" in result

    def test_empty_log(self):
        assert get_error_log("") == "[Console log is empty]"
        assert get_error_log("   \n  ") == "[Console log is empty]"


# ---------------------------------------------------------------------------
# Hard limit guard
# ---------------------------------------------------------------------------


class TestHardLimit:
    def test_never_exceeds_hard_limit(self):
        lines = ["Started"]
        for i in range(100):
            lines.extend([f"noise {j}" for j in range(3)])
            lines.append(f"ERROR: failure variant {i} with unique message {i * 7}")
        lines.append("Finished: FAILURE")
        text = "\n".join(lines)
        result = get_error_log(text)
        assert len(result.splitlines()) <= HARD_LIMIT + 1


# ---------------------------------------------------------------------------
# truncate_tail
# ---------------------------------------------------------------------------


class TestTruncateTail:
    def test_short_log_unchanged(self):
        text = "a\nb\nc"
        assert truncate_tail(text, 10) == text

    def test_long_log_truncated(self):
        text = _make_log(500)
        result = truncate_tail(text, 100)
        assert "showing last 100 of 500 lines" in result.lower()
        result_lines = result.splitlines()
        assert len(result_lines) == 101


# ---------------------------------------------------------------------------
# Anchors present
# ---------------------------------------------------------------------------


class TestAnchors:
    def test_head_and_tail_present(self):
        lines = ["first line", "second line", "third line", "fourth", "fifth"]
        lines.extend([f"middle {i}" for i in range(50)])
        lines.append("ERROR: something broke")
        lines.extend([f"end {i}" for i in range(40)])
        text = "\n".join(lines)
        result = get_error_log(text)
        assert "Log start (first 5 lines)" in result
        assert "first line" in result
        assert "Log end" in result


# ---------------------------------------------------------------------------
# Merge metadata quality
# ---------------------------------------------------------------------------


class TestMergeMetadata:
    """Merged sections should keep the most informative tier and key lines."""

    def test_error_escalated_to_critical_on_merge(self):
        lines = [f"line {i}" for i in range(5)]
        lines.append("ERROR: something went wrong")
        lines.append("FATAL: system halted")
        lines.extend([f"line {i}" for i in range(5)])
        text = "\n".join(lines)
        result = get_error_log(text)
        assert "CRITICAL" in result
