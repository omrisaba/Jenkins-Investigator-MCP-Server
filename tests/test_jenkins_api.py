"""Tests for the three new jenkins_api functions: pipeline stages, parameters, build history."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from utils.jenkins_api import (
    _MAX_HISTORY,
    extract_parameters,
    get_build_history,
    get_pipeline_stages,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _raise_404(*args, **kwargs):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = 404
    raise requests.HTTPError(response=resp)


# ---------------------------------------------------------------------------
# get_pipeline_stages
# ---------------------------------------------------------------------------


class TestGetPipelineStages:
    @patch("utils.jenkins_api._get")
    def test_happy_path(self, mock_get):
        mock_get.return_value = _mock_response({
            "stages": [
                {"id": "6", "name": "Build", "status": "SUCCESS", "durationMillis": 45000},
                {"id": "11", "name": "Test", "status": "FAILED", "durationMillis": 120000},
                {"id": "18", "name": "Deploy", "status": "NOT_EXECUTED", "durationMillis": 0},
            ]
        })
        result = get_pipeline_stages("my-pipeline", 42)
        assert result is not None
        assert len(result) == 3
        assert result[0] == {"id": "6", "name": "Build", "status": "SUCCESS", "duration_s": 45.0}
        assert result[1] == {"id": "11", "name": "Test", "status": "FAILED", "duration_s": 120.0}
        assert result[2] == {"id": "18", "name": "Deploy", "status": "NOT_EXECUTED", "duration_s": 0.0}

    @patch("utils.jenkins_api._get")
    def test_freestyle_returns_none(self, mock_get):
        mock_get.side_effect = _raise_404
        result = get_pipeline_stages("freestyle-job", 10)
        assert result is None

    @patch("utils.jenkins_api._get")
    def test_empty_stages(self, mock_get):
        mock_get.return_value = _mock_response({"stages": []})
        result = get_pipeline_stages("my-pipeline", 5)
        assert result == []

    @patch("utils.jenkins_api._get")
    def test_missing_stages_key(self, mock_get):
        mock_get.return_value = _mock_response({})
        result = get_pipeline_stages("my-pipeline", 5)
        assert result == []


# ---------------------------------------------------------------------------
# extract_parameters
# ---------------------------------------------------------------------------


class TestExtractParameters:
    def test_typical_params(self):
        actions = [
            {"_class": "hudson.model.CauseAction", "causes": []},
            {
                "_class": "hudson.model.ParametersAction",
                "parameters": [
                    {"name": "BRANCH", "value": "main"},
                    {"name": "ENVIRONMENT", "value": "staging"},
                    {"name": "DRY_RUN", "value": True},
                ],
            },
        ]
        result = extract_parameters(actions)
        assert len(result) == 3
        assert result[0] == {"name": "BRANCH", "value": "main"}
        assert result[1] == {"name": "ENVIRONMENT", "value": "staging"}
        assert result[2] == {"name": "DRY_RUN", "value": "True"}

    def test_empty_actions(self):
        assert extract_parameters([]) == []

    def test_no_parameters_action(self):
        actions = [{"_class": "hudson.model.CauseAction"}]
        assert extract_parameters(actions) == []

    def test_large_value_truncated(self):
        big_value = "x" * 300
        actions = [{
            "_class": "hudson.model.ParametersAction",
            "parameters": [{"name": "CONFIG", "value": big_value}],
        }]
        result = extract_parameters(actions)
        assert len(result) == 1
        assert len(result[0]["value"]) < 300
        assert result[0]["value"].endswith("…[truncated]")

    def test_none_value(self):
        actions = [{
            "_class": "hudson.model.ParametersAction",
            "parameters": [{"name": "OPT", "value": None}],
        }]
        result = extract_parameters(actions)
        assert result[0]["value"] == "None"


# ---------------------------------------------------------------------------
# get_build_history
# ---------------------------------------------------------------------------


class TestGetBuildHistory:
    @patch("utils.jenkins_api._get")
    def test_normal_history(self, mock_get):
        mock_get.return_value = _mock_response({
            "builds": [
                {"number": 100, "result": "FAILURE", "duration": 30000, "timestamp": 1700000000000},
                {"number": 99, "result": "SUCCESS", "duration": 25000, "timestamp": 1699990000000},
                {"number": 98, "result": "SUCCESS", "duration": 28000, "timestamp": 1699980000000},
            ]
        })
        result = get_build_history("my-job", 3)
        assert len(result) == 3
        assert result[0]["number"] == 100
        assert result[0]["result"] == "FAILURE"
        assert result[0]["duration_s"] == 30.0
        assert result[1]["result"] == "SUCCESS"

    @patch("utils.jenkins_api._get")
    def test_empty_job(self, mock_get):
        mock_get.return_value = _mock_response({"builds": []})
        result = get_build_history("new-job", 10)
        assert result == []

    @patch("utils.jenkins_api._get")
    def test_count_clamped_to_max(self, mock_get):
        mock_get.return_value = _mock_response({"builds": []})
        get_build_history("my-job", 999)
        call_args = mock_get.call_args[0][0]
        assert f"{{0,{_MAX_HISTORY}}}" in call_args

    @patch("utils.jenkins_api._get")
    def test_count_minimum_one(self, mock_get):
        mock_get.return_value = _mock_response({"builds": []})
        get_build_history("my-job", -5)
        call_args = mock_get.call_args[0][0]
        assert "{0,1}" in call_args

    @patch("utils.jenkins_api._get")
    def test_in_progress_build(self, mock_get):
        mock_get.return_value = _mock_response({
            "builds": [
                {"number": 50, "result": None, "duration": 0, "timestamp": 1700000000000},
            ]
        })
        result = get_build_history("my-job", 1)
        assert result[0]["result"] == "IN_PROGRESS"
