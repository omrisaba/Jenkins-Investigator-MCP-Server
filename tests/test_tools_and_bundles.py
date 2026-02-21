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
