"""Tests for new MCP tools (stage resolution, search) and bundle output caps."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import re

import pytest
import requests

from server import _resolve_stage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


SAMPLE_STAGES = [
    {"id": "6", "name": "Build", "status": "SUCCESS", "duration_s": 30.0},
    {"id": "11", "name": "Unit Tests", "status": "FAILED", "duration_s": 60.0},
    {"id": "18", "name": "Integration Tests", "status": "NOT_EXECUTED", "duration_s": 0.0},
    {"id": "25", "name": "Deploy", "status": "NOT_EXECUTED", "duration_s": 0.0},
]


# ---------------------------------------------------------------------------
# _resolve_stage — three-tier matching
# ---------------------------------------------------------------------------


class TestResolveStage:
    def test_exact_match(self):
        result = _resolve_stage(SAMPLE_STAGES, "Build")
        assert isinstance(result, dict)
        assert result["name"] == "Build"

    def test_case_insensitive_match(self):
        result = _resolve_stage(SAMPLE_STAGES, "build")
        assert isinstance(result, dict)
        assert result["name"] == "Build"

    def test_case_insensitive_match_unique(self):
        result = _resolve_stage(SAMPLE_STAGES, "deploy")
        assert isinstance(result, dict)
        assert result["name"] == "Deploy"

    def test_substring_match_unique(self):
        result = _resolve_stage(SAMPLE_STAGES, "Unit")
        assert isinstance(result, dict)
        assert result["name"] == "Unit Tests"

    def test_ambiguous_substring(self):
        result = _resolve_stage(SAMPLE_STAGES, "Tests")
        assert isinstance(result, str)
        assert "Ambiguous" in result
        assert "Unit Tests" in result
        assert "Integration Tests" in result

    def test_no_match(self):
        result = _resolve_stage(SAMPLE_STAGES, "Publish")
        assert isinstance(result, str)
        assert "not found" in result
        assert "Build" in result

    def test_empty_stages(self):
        result = _resolve_stage([], "Build")
        assert isinstance(result, str)
        assert "not found" in result


# ---------------------------------------------------------------------------
# Log parser parameterization
# ---------------------------------------------------------------------------


class TestLogParserParameterization:
    def test_custom_budget(self):
        from utils.log_parser import get_error_log
        lines = ["ok"] * 100
        lines[50] = "FATAL error occurred"
        lines.extend(["ok"] * 100)
        text = "\n".join(lines)

        result = get_error_log(text, max_lines=50, hard_limit=60)
        assert len(result.splitlines()) <= 60

    def test_no_head(self):
        from utils.log_parser import get_error_log
        lines = ["setup line"] * 5
        lines.append("ERROR: something broke")
        lines.extend(["more output"] * 20)
        text = "\n".join(lines)

        result = get_error_log(text, include_head=False)
        assert "Log start" not in result

    def test_no_tail(self):
        from utils.log_parser import get_error_log
        lines = ["ERROR: crash at start"]
        lines.extend(["ok"] * 200)
        text = "\n".join(lines)

        result = get_error_log(text, include_tail=False)
        assert "Log end" not in result

    def test_defaults_unchanged(self):
        from utils.log_parser import get_error_log
        lines = ["ok"] * 5
        lines.append("ERROR: test failure")
        lines.extend(["ok"] * 50)
        text = "\n".join(lines)

        result = get_error_log(text)
        assert "Log start" in result
        assert "Log end" in result


# ---------------------------------------------------------------------------
# Bundle output cap enforcement
# ---------------------------------------------------------------------------


class TestBundleOutputCaps:
    @patch("utils.jenkins_api.get_named_build")
    @patch("utils.jenkins_api.get_pipeline_stages")
    @patch("utils.jenkins_api.get_console_text")
    @patch("utils.jenkins_api.get_test_report")
    @patch("utils.jenkins_api.get_build_history")
    @patch("server.TOOL_DELAY", 0)
    @patch("server._BUNDLE_PACING", 0)
    def test_investigate_respects_hard_cap(
        self, mock_history, mock_test, mock_console, mock_stages, mock_build
    ):
        from server import investigate_build_failure, _INVESTIGATE_HARD_CAP

        mock_build.return_value = {
            "number": 42, "result": "FAILURE", "duration": 30000,
            "timestamp": 1700000000000, "builtOn": "node1",
            "actions": [], "changeSet": {"items": []},
        }
        mock_stages.return_value = [
            {"id": "1", "name": f"Stage{i}", "status": "FAILED", "duration_s": 1.0}
            for i in range(20)
        ]
        mock_console.return_value = "\n".join(
            [f"ERROR: line {i}" for i in range(500)]
        )
        mock_test.return_value = {
            "fail_count": 50, "pass_count": 100, "skip_count": 0,
            "failing_tests": [
                {"class_name": f"C{i}", "test_name": f"t{i}", "error_details": f"err{i}", "error_stack_trace": ""}
                for i in range(50)
            ],
        }
        mock_history.return_value = [
            {"number": 42 - i, "result": "FAILURE", "duration_s": 1.0, "timestamp": 1700000000000, "agent": "n"}
            for i in range(10)
        ]

        result = investigate_build_failure("job")
        assert len(result.splitlines()) <= _INVESTIGATE_HARD_CAP + 1

    @patch("utils.jenkins_api.get_named_build")
    @patch("utils.jenkins_api.get_build_history")
    @patch("utils.jenkins_api.get_build")
    @patch("server.TOOL_DELAY", 0)
    @patch("server._BUNDLE_PACING", 0)
    def test_compare_respects_hard_cap(
        self, mock_get_build, mock_history, mock_named
    ):
        from server import compare_failing_vs_passing, _COMPARE_HARD_CAP

        mock_named.return_value = {"number": 100}
        mock_history.return_value = [
            {"number": 100 - i, "result": "FAILURE" if i < 5 else "SUCCESS",
             "duration_s": 1.0, "timestamp": 1700000000000, "agent": "n"}
            for i in range(25)
        ]
        mock_get_build.return_value = {
            "result": "FAILURE", "duration": 1000, "timestamp": 1700000000000,
            "builtOn": "node1", "actions": [],
            "changeSet": {"items": [
                {"commitId": f"abc{i}", "author": {"fullName": "dev"}, "msg": f"commit {i}"}
                for i in range(20)
            ]},
        }

        result = compare_failing_vs_passing("job")
        assert len(result.splitlines()) <= _COMPARE_HARD_CAP + 1


# ---------------------------------------------------------------------------
# Bundle short-circuit on missing build
# ---------------------------------------------------------------------------


class TestBundleShortCircuit:
    @patch("utils.jenkins_api.get_named_build")
    @patch("server.TOOL_DELAY", 0)
    def test_investigate_no_build_found(self, mock_build):
        from server import investigate_build_failure
        mock_build.return_value = {}

        result = investigate_build_failure("nonexistent-job")
        assert "No 'last_failed' build" in result

    @patch("utils.jenkins_api.get_named_build")
    @patch("utils.jenkins_api.get_build_history")
    @patch("server.TOOL_DELAY", 0)
    @patch("server._BUNDLE_PACING", 0)
    def test_compare_no_passing_build(self, mock_history, mock_named):
        from server import compare_failing_vs_passing
        mock_named.return_value = {"number": 100}
        mock_history.return_value = [
            {"number": 100 - i, "result": "FAILURE", "duration_s": 1.0,
             "timestamp": 1700000000000, "agent": "n"}
            for i in range(25)
        ]

        result = compare_failing_vs_passing("job")
        assert "No passing build" in result


# ---------------------------------------------------------------------------
# deep_dive_test_failures — regression detection
# ---------------------------------------------------------------------------


def _make_test_report(failing_keys: list[str], pass_count: int = 10) -> dict:
    """Build a test report dict with the given failing test keys (ClassName.testName)."""
    failing_tests = []
    for key in failing_keys:
        cls, name = key.rsplit(".", 1)
        failing_tests.append({
            "class_name": cls, "test_name": name,
            "error_details": f"assertion failed in {name}",
            "error_stack_trace": "",
        })
    return {
        "fail_count": len(failing_keys),
        "pass_count": pass_count,
        "skip_count": 0,
        "failing_tests": failing_tests,
    }


@patch("server._enrich_with_junit_xml", return_value=None)
class TestDeepDiveRegression:
    """Verify regression detection identifies the correct PASS→FAIL transition."""

    @patch("utils.jenkins_api.get_build")
    @patch("utils.jenkins_api.get_test_report")
    @patch("utils.jenkins_api.get_build_history")
    @patch("server.TOOL_DELAY", 0)
    @patch("server._BUNDLE_PACING", 0)
    def test_pass_to_fail_transition(self, mock_history, mock_test_report, mock_get_build, _mock_xml):
        """Test passes in 97-98, fails in 99-100.  Regression should be build #99."""
        from server import deep_dive_test_failures

        test_key = "com.example.AppTest.testLogin"

        def test_report_side_effect(job, bn):
            if bn == 100:
                return _make_test_report([test_key])
            if bn in (99,):
                return _make_test_report([test_key])
            return _make_test_report([])

        mock_test_report.side_effect = test_report_side_effect
        mock_history.return_value = [
            {"number": n, "result": "FAILURE" if n >= 99 else "SUCCESS",
             "duration_s": 1.0, "timestamp": 1700000000000, "agent": "n"}
            for n in [100, 99, 98, 97, 96]
        ]
        mock_get_build.return_value = {
            "result": "FAILURE", "duration": 1000, "timestamp": 1700000000000,
            "builtOn": "node1", "actions": [],
            "changeSets": [{"items": [
                {"commitId": "abc123def456", "author": {"fullName": "Alice"}, "comment": "refactored auth"}
            ]}],
        }

        result = deep_dive_test_failures("job", 100)
        assert "Regression started at build #99" in result
        assert "abc123def45" in result
        assert "Alice" in result

    @patch("utils.jenkins_api.get_build")
    @patch("utils.jenkins_api.get_test_report")
    @patch("utils.jenkins_api.get_build_history")
    @patch("server.TOOL_DELAY", 0)
    @patch("server._BUNDLE_PACING", 0)
    def test_all_failing_persistent(self, mock_history, mock_test_report, mock_get_build, _mock_xml):
        """Test fails in all prior builds.  Should report 'Persistent failure'."""
        from server import deep_dive_test_failures

        test_key = "com.example.AppTest.testLogin"
        mock_test_report.return_value = _make_test_report([test_key])
        mock_history.return_value = [
            {"number": n, "result": "FAILURE",
             "duration_s": 1.0, "timestamp": 1700000000000, "agent": "n"}
            for n in [100, 99, 98, 97, 96]
        ]
        mock_get_build.return_value = {
            "result": "FAILURE", "duration": 1000, "timestamp": 1700000000000,
            "builtOn": "node1", "actions": [], "changeSet": {"items": []},
        }

        result = deep_dive_test_failures("job", 100)
        assert "Persistent failure" in result
        assert "NEW failure" not in result

    @patch("utils.jenkins_api.get_build")
    @patch("utils.jenkins_api.get_test_report")
    @patch("utils.jenkins_api.get_build_history")
    @patch("server.TOOL_DELAY", 0)
    @patch("server._BUNDLE_PACING", 0)
    def test_only_current_build_fails(self, mock_history, mock_test_report, mock_get_build, _mock_xml):
        """Test passes in all prior builds, fails only in current.  Should report 'NEW failure'."""
        from server import deep_dive_test_failures

        test_key = "com.example.AppTest.testLogin"

        def test_report_side_effect(job, bn):
            if bn == 100:
                return _make_test_report([test_key])
            return _make_test_report([])

        mock_test_report.side_effect = test_report_side_effect
        mock_history.return_value = [
            {"number": n, "result": "SUCCESS" if n < 100 else "FAILURE",
             "duration_s": 1.0, "timestamp": 1700000000000, "agent": "n"}
            for n in [100, 99, 98, 97, 96]
        ]
        mock_get_build.return_value = {
            "result": "FAILURE", "duration": 1000, "timestamp": 1700000000000,
            "builtOn": "node1", "actions": [], "changeSet": {"items": []},
        }

        result = deep_dive_test_failures("job", 100)
        assert "NEW failure" in result
        assert "Persistent failure" not in result
        assert "Regression started" not in result


# ---------------------------------------------------------------------------
# compare_failing_vs_passing — duration diff
# ---------------------------------------------------------------------------


class TestCompareDurationDiff:
    @patch("utils.jenkins_api.get_named_build")
    @patch("utils.jenkins_api.get_build_history")
    @patch("utils.jenkins_api.get_build")
    @patch("server.TOOL_DELAY", 0)
    @patch("server._BUNDLE_PACING", 0)
    def test_duration_diff_shown_when_large(self, mock_get_build, mock_history, mock_named):
        from server import compare_failing_vs_passing

        mock_named.return_value = {"number": 50}
        mock_history.return_value = [
            {"number": 50, "result": "FAILURE", "duration_s": 120.0,
             "timestamp": 1700000000000, "agent": "n"},
            {"number": 49, "result": "SUCCESS", "duration_s": 30.0,
             "timestamp": 1700000000000, "agent": "n"},
        ]

        def build_side_effect(job, bn):
            if bn == 50:
                return {
                    "result": "FAILURE", "duration": 120000,
                    "timestamp": 1700000000000, "builtOn": "node1",
                    "actions": [], "changeSet": {"items": []},
                }
            return {
                "result": "SUCCESS", "duration": 30000,
                "timestamp": 1700000000000, "builtOn": "node1",
                "actions": [], "changeSet": {"items": []},
            }

        mock_get_build.side_effect = build_side_effect

        result = compare_failing_vs_passing("job")
        assert "DURATION DIFF" in result
        assert "+90.0s" in result

    @patch("utils.jenkins_api.get_named_build")
    @patch("utils.jenkins_api.get_build_history")
    @patch("utils.jenkins_api.get_build")
    @patch("server.TOOL_DELAY", 0)
    @patch("server._BUNDLE_PACING", 0)
    def test_duration_diff_hidden_when_small(self, mock_get_build, mock_history, mock_named):
        from server import compare_failing_vs_passing

        mock_named.return_value = {"number": 50}
        mock_history.return_value = [
            {"number": 50, "result": "FAILURE", "duration_s": 35.0,
             "timestamp": 1700000000000, "agent": "n"},
            {"number": 49, "result": "SUCCESS", "duration_s": 30.0,
             "timestamp": 1700000000000, "agent": "n"},
        ]
        mock_get_build.return_value = {
            "result": "FAILURE", "duration": 35000,
            "timestamp": 1700000000000, "builtOn": "node1",
            "actions": [], "changeSet": {"items": []},
        }

        result = compare_failing_vs_passing("job")
        assert "DURATION DIFF" not in result


# ---------------------------------------------------------------------------
# search_across_jobs bundle
# ---------------------------------------------------------------------------


def _make_folder_jobs(names_and_colors):
    """Build a list of job dicts matching get_folder_jobs(include_last_failed=True) output."""
    jobs = []
    for name, color, last_bn, result in names_and_colors:
        jobs.append({
            "name": name,
            "color": color,
            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
            "last_build_number": last_bn,
            "last_result": result,
            "last_timestamp": 1700000000000,
            "last_failed_build_number": last_bn if color.startswith("red") else None,
        })
    return jobs


class TestSearchAcrossJobs:
    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_finds_matches_across_jobs(self, mock_folder, mock_tail):
        from server import search_across_jobs

        mock_folder.return_value = _make_folder_jobs([
            ("job-a", "red", 10, "FAILURE"),
            ("job-b", "blue", 20, "SUCCESS"),
            ("job-c", "red", 30, "FAILURE"),
        ])
        mock_tail.side_effect = lambda job, bn, max_bytes=500000: {
            "my-folder/job-a": "line 1\nERROR: NullPointerException\nline 3",
            "my-folder/job-b": "all good\nno errors here",
            "my-folder/job-c": "setup\nERROR: NullPointerException in service\ncleanup",
        }.get(job, "")

        result = search_across_jobs("my-folder", "NullPointerException")
        assert "Matched: 2 jobs" in result
        assert "job-a" in result
        assert "job-c" in result
        assert "NullPointerException" in result

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_no_matches_reports_zero(self, mock_folder, mock_tail):
        from server import search_across_jobs

        mock_folder.return_value = _make_folder_jobs([
            ("job-a", "blue", 10, "SUCCESS"),
        ])
        mock_tail.return_value = "everything is fine\nno problems"

        result = search_across_jobs("my-folder", "FatalCrash")
        assert "Matched: 0 jobs" in result
        assert "Total hits: 0" in result

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_filter_status_failing(self, mock_folder, mock_tail):
        from server import search_across_jobs

        mock_folder.return_value = _make_folder_jobs([
            ("failing-job", "red", 10, "FAILURE"),
            ("passing-job", "blue", 20, "SUCCESS"),
        ])
        mock_tail.return_value = "ERROR: something broke"

        result = search_across_jobs(
            "my-folder", "ERROR", filter_status="failing",
        )
        assert "Searched: 1 jobs" in result

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_filter_status_unstable_and_failing(self, mock_folder, mock_tail):
        from server import search_across_jobs

        mock_folder.return_value = _make_folder_jobs([
            ("failing-job", "red", 10, "FAILURE"),
            ("unstable-job", "yellow", 20, "UNSTABLE"),
            ("passing-job", "blue", 30, "SUCCESS"),
        ])
        mock_tail.return_value = "ERROR: something broke"

        result = search_across_jobs(
            "my-folder", "ERROR", filter_status="unstable_and_failing",
        )
        assert "Searched: 2 jobs" in result

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_build_selector_last_failed(self, mock_folder, mock_tail):
        from server import search_across_jobs

        mock_folder.return_value = [{
            "name": "job-x", "color": "blue",
            "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob",
            "last_build_number": 50,
            "last_result": "SUCCESS",
            "last_timestamp": 1700000000000,
            "last_failed_build_number": 48,
        }]
        mock_tail.return_value = "ERROR: old failure"

        result = search_across_jobs(
            "folder", "ERROR", build_selector="last_failed",
        )
        assert "Matched: 1 jobs" in result
        assert "(FAILURE)" in result
        assert "(SUCCESS)" not in result
        mock_tail.assert_called_once_with("folder/job-x", 48, 500000)

    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_empty_folder(self, mock_folder):
        from server import search_across_jobs

        mock_folder.return_value = []
        result = search_across_jobs("empty-folder", "ERROR")
        assert "No jobs with matching builds" in result

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_output_cap_enforced(self, mock_folder, mock_tail):
        from server import search_across_jobs, _SEARCH_ACROSS_OUTPUT_CAP

        mock_folder.return_value = _make_folder_jobs([
            (f"job-{i}", "red", i + 1, "FAILURE") for i in range(20)
        ])
        mock_tail.return_value = "\n".join(
            [f"ERROR: failure line {i}" for i in range(100)]
        )

        result = search_across_jobs("big-folder", "ERROR")
        assert len(result.splitlines()) <= _SEARCH_ACROSS_OUTPUT_CAP + 1

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_regex_pattern(self, mock_folder, mock_tail):
        from server import search_across_jobs

        mock_folder.return_value = _make_folder_jobs([
            ("job-a", "red", 10, "FAILURE"),
        ])
        mock_tail.return_value = "NPE at com.example.Foo.bar(Foo.java:42)"

        result = search_across_jobs(
            "folder", r"NPE at .+\.java:\d+", is_regex=True,
        )
        assert "Matched: 1 jobs" in result

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_fetch_error_counted(self, mock_folder, mock_tail):
        from server import search_across_jobs

        mock_folder.return_value = _make_folder_jobs([
            ("broken-job", "red", 10, "FAILURE"),
        ])
        mock_tail.side_effect = Exception("timeout")

        result = search_across_jobs("folder", "ERROR")
        assert "1 jobs had errors" in result

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_adjacent_matches_merged(self, mock_folder, mock_tail):
        """Two matches 1 line apart should produce a single merged snippet."""
        from server import search_across_jobs

        mock_folder.return_value = _make_folder_jobs([
            ("job-a", "red", 10, "FAILURE"),
        ])
        lines = ["ok"] * 10
        lines[4] = "ERROR: first problem"
        lines[5] = "ERROR: second problem"
        mock_tail.return_value = "\n".join(lines)

        result = search_across_jobs("folder", "ERROR", context_lines=2)
        snippets = [l for l in result.splitlines() if l.startswith(">>>") or l.startswith("   ")]
        line_numbers = []
        for s in snippets:
            parts = s.strip().split("L", 1)
            if len(parts) > 1:
                ln = parts[1].split(":")[0]
                line_numbers.append(int(ln))
        assert line_numbers, "Expected snippet lines in output"
        assert len(line_numbers) == len(set(line_numbers)), "Merged snippet should have no duplicate lines"

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server._SEARCH_ACROSS_MAX_JOBS", 2)
    @patch("server.TOOL_DELAY", 0)
    def test_failing_jobs_searched_first(self, mock_folder, mock_tail):
        """When capped, failing/unstable jobs should be searched over passing ones."""
        from server import search_across_jobs

        mock_folder.return_value = _make_folder_jobs([
            ("passing-job", "blue", 20, "SUCCESS"),
            ("failing-job", "red", 10, "FAILURE"),
            ("unstable-job", "yellow", 30, "UNSTABLE"),
        ])
        mock_tail.return_value = "ERROR: found it"

        result = search_across_jobs("folder", "ERROR")
        assert "Searched: 2 jobs" in result
        assert "failing-job" in result
        assert "unstable-job" in result
        assert "passing-job" not in result.split("===")[1]
        assert "1 jobs not searched" in result

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_recursive_searches_subfolders(self, mock_folder, mock_tail):
        from server import search_across_jobs

        root_jobs = [
            {
                "name": "sub1", "color": "", "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                "last_build_number": None, "last_result": None,
                "last_timestamp": None, "last_failed_build_number": None,
            },
        ]
        sub_jobs = _make_folder_jobs([
            ("nested-job", "red", 5, "FAILURE"),
        ])

        mock_folder.side_effect = [root_jobs, sub_jobs]
        mock_tail.return_value = "ERROR: nested error"

        result = search_across_jobs("parent", "ERROR", recursive=True)
        assert "nested-job" in result
        assert "Matched: 1 jobs" in result
        assert mock_folder.call_count == 2
        mock_folder.assert_any_call("parent/sub1", include_last_failed=True)

    @patch("utils.jenkins_api.get_console_text_tail")
    @patch("utils.jenkins_api.get_folder_jobs")
    @patch("server.TOOL_DELAY", 0)
    def test_subfolder_hint_when_not_recursive(self, mock_folder, mock_tail):
        from server import search_across_jobs

        mock_folder.return_value = [
            {
                "name": "sub1", "color": "", "_class": "com.cloudbees.hudson.plugins.folder.Folder",
                "last_build_number": None, "last_result": None,
                "last_timestamp": None, "last_failed_build_number": None,
            },
        ] + _make_folder_jobs([("job-a", "red", 10, "FAILURE")])
        mock_tail.return_value = "ERROR: something"

        result = search_across_jobs("parent", "ERROR", recursive=False)
        assert "subfolders not searched" in result
        assert "recursive=true" in result
