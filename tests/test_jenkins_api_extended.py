"""Tests for the new jenkins_api functions added in the expansion."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest
import requests

from utils.jenkins_api import (
    _job_path,
    _strip_html,
    get_stage_log,
    get_flow_node_detail,
    get_artifacts_list,
    get_artifact_content,
    get_job_config_xml,
    get_queue,
    get_folder_jobs,
    get_all_nodes,
    get_injected_env_vars,
    get_build_history,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(json_data: dict | None = None, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    resp.raise_for_status.return_value = None
    return resp


def _raise_http(status: int):
    def _side_effect(*args, **kwargs):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = status
        raise requests.HTTPError(response=resp)
    return _side_effect


# ---------------------------------------------------------------------------
# _job_path URL encoding
# ---------------------------------------------------------------------------


class TestJobPath:
    def test_simple_job(self):
        assert _job_path("my-job") == "/job/my-job"

    def test_folder_path(self):
        assert _job_path("org/repo/main") == "/job/org/job/repo/job/main"

    def test_spaces_encoded(self):
        assert _job_path("My Job") == "/job/My%20Job"

    def test_special_chars_encoded(self):
        result = _job_path("team/feat#123")
        assert "%23" in result

    def test_percent_encoded(self):
        result = _job_path("job%name")
        assert "%25" in result


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------


class TestStripHtml:
    def test_removes_tags(self):
        assert _strip_html("<b>error</b> happened") == "error happened"

    def test_plain_text_unchanged(self):
        assert _strip_html("no tags here") == "no tags here"

    def test_nested_tags(self):
        assert _strip_html("<div><span>msg</span></div>") == "msg"

    def test_empty_string(self):
        assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# get_stage_log
# ---------------------------------------------------------------------------


class TestGetStageLog:
    @patch("utils.jenkins_api._get")
    def test_single_page(self, mock_get):
        mock_get.return_value = _mock_response({
            "text": "<b>Step</b>: running tests",
            "hasMore": False,
            "length": 100,
        })
        result = get_stage_log("job", 1, "6")
        assert "Step" in result
        assert "<b>" not in result

    @patch("utils.jenkins_api._get")
    def test_pagination(self, mock_get):
        page1 = _mock_response({"text": "page1 ", "hasMore": True, "length": 100})
        page2 = _mock_response({"text": "page2", "hasMore": False, "length": 200})
        mock_get.side_effect = [page1, page2]

        result = get_stage_log("job", 1, "6")
        assert "page1" in result
        assert "page2" in result
        assert mock_get.call_count == 2

    @patch("utils.jenkins_api._get")
    def test_404_returns_empty(self, mock_get):
        mock_get.side_effect = _raise_http(404)
        result = get_stage_log("job", 1, "6")
        assert result == ""

    @patch("utils.jenkins_api._get")
    def test_pagination_guard_against_infinite_loop(self, mock_get):
        """If length never advances, we break out to avoid infinite loop."""
        stuck_page = _mock_response({"text": "x", "hasMore": True, "length": 0})
        mock_get.return_value = stuck_page
        result = get_stage_log("job", 1, "6")
        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# get_flow_node_detail
# ---------------------------------------------------------------------------


class TestGetFlowNodeDetail:
    @patch("utils.jenkins_api._get")
    def test_happy_path(self, mock_get):
        mock_get.return_value = _mock_response({
            "id": "6",
            "name": "Test",
            "status": "FAILED",
            "durationMillis": 30000,
            "stageFlowNodes": [
                {"id": "7", "name": "sh", "status": "SUCCESS", "durationMillis": 5000},
                {"id": "8", "name": "junit", "status": "FAILED", "durationMillis": 2000},
            ],
        })
        result = get_flow_node_detail("job", 1, "6")
        assert result["id"] == "6"
        assert result["status"] == "FAILED"
        assert len(result["stage_flow_nodes"]) == 2

    @patch("utils.jenkins_api._get")
    def test_404_returns_none(self, mock_get):
        mock_get.side_effect = _raise_http(404)
        assert get_flow_node_detail("job", 1, "6") is None


# ---------------------------------------------------------------------------
# get_artifacts_list
# ---------------------------------------------------------------------------


class TestGetArtifactsList:
    @patch("utils.jenkins_api._get")
    def test_happy_path(self, mock_get):
        mock_get.return_value = _mock_response({
            "artifacts": [
                {"relativePath": "target/app.jar", "fileName": "app.jar"},
                {"relativePath": "logs/test.log", "fileName": "test.log"},
            ]
        })
        result = get_artifacts_list("job", 1)
        assert len(result) == 2
        assert result[0]["file_name"] == "app.jar"

    @patch("utils.jenkins_api._get")
    def test_no_artifacts(self, mock_get):
        mock_get.return_value = _mock_response({"artifacts": []})
        assert get_artifacts_list("job", 1) == []


# ---------------------------------------------------------------------------
# get_artifact_content
# ---------------------------------------------------------------------------


class TestGetArtifactContent:
    def test_binary_extension_rejected(self):
        result = get_artifact_content("job", 1, "output/app.jar")
        assert result is None

    @patch("utils.jenkins_api._get")
    def test_text_file_returned(self, mock_get):
        resp = MagicMock()
        resp.iter_content.return_value = [b"log line 1\nlog line 2"]
        resp.close = MagicMock()
        mock_get.return_value = resp

        result = get_artifact_content("job", 1, "logs/test.log")
        assert "log line 1" in result

    @patch("utils.jenkins_api._get")
    def test_truncation_marker(self, mock_get):
        resp = MagicMock()
        resp.iter_content.return_value = [b"x" * 60000]
        resp.close = MagicMock()
        mock_get.return_value = resp

        result = get_artifact_content("job", 1, "logs/big.txt", max_bytes=1024)
        assert "TRUNCATED" in result


# ---------------------------------------------------------------------------
# get_queue
# ---------------------------------------------------------------------------


class TestGetQueue:
    @patch("utils.jenkins_api._get")
    def test_empty_queue(self, mock_get):
        mock_get.return_value = _mock_response({"items": []})
        assert get_queue() == []

    @patch("utils.jenkins_api._get")
    def test_filter_by_job(self, mock_get):
        mock_get.return_value = _mock_response({
            "items": [
                {"id": 1, "task": {"name": "my-job"}, "why": "Waiting", "blocked": False, "stuck": False, "inQueueSince": 0},
                {"id": 2, "task": {"name": "other-job"}, "why": "Waiting", "blocked": False, "stuck": False, "inQueueSince": 0},
            ]
        })
        result = get_queue(job_filter="my-job")
        assert len(result) == 1
        assert result[0]["task_name"] == "my-job"


# ---------------------------------------------------------------------------
# get_folder_jobs
# ---------------------------------------------------------------------------


class TestGetFolderJobs:
    @patch("utils.jenkins_api._get")
    def test_root_listing(self, mock_get):
        mock_get.return_value = _mock_response({
            "jobs": [
                {"name": "job1", "color": "blue", "url": "http://j/job1", "_class": "org.jenkinsci.plugins.workflow.job.WorkflowJob", "lastBuild": {"number": 10, "result": "SUCCESS", "timestamp": 1700000000000}},
                {"name": "folder1", "color": "blue", "url": "http://j/folder1", "_class": "com.cloudbees.hudson.plugins.folder.Folder", "lastBuild": None},
            ]
        })
        result = get_folder_jobs()
        assert len(result) == 2
        assert result[0]["name"] == "job1"
        assert result[0]["last_build_number"] == 10

    @patch("utils.jenkins_api._get")
    def test_folder_path_used(self, mock_get):
        mock_get.return_value = _mock_response({"jobs": []})
        get_folder_jobs("my-org/my-team")
        path = mock_get.call_args[0][0]
        assert "/job/my-org/job/my-team/" in path


# ---------------------------------------------------------------------------
# get_all_nodes
# ---------------------------------------------------------------------------


class TestGetAllNodes:
    @patch("utils.jenkins_api._get")
    def test_parses_nodes(self, mock_get):
        mock_get.return_value = _mock_response({
            "computer": [
                {
                    "displayName": "master",
                    "offline": False,
                    "offlineCauseReason": None,
                    "assignedLabels": [{"name": "master"}, {"name": "built-in"}],
                    "numExecutors": 2,
                    "idle": True,
                    "monitorData": {
                        "hudson.node_monitors.DiskSpaceMonitor": {"size": 107374182400}
                    },
                },
            ]
        })
        result = get_all_nodes()
        assert len(result) == 1
        assert result[0]["name"] == "master"
        assert result[0]["online"] is True
        assert result[0]["disk_gb"] == 100.0
        assert "master" in result[0]["labels"]


# ---------------------------------------------------------------------------
# get_injected_env_vars
# ---------------------------------------------------------------------------


class TestGetInjectedEnvVars:
    @patch("utils.jenkins_api._get")
    def test_happy_path(self, mock_get):
        mock_get.return_value = _mock_response({"envMap": {"BUILD_ID": "123", "JOB_NAME": "foo"}})
        result = get_injected_env_vars("foo", 1)
        assert result == {"BUILD_ID": "123", "JOB_NAME": "foo"}

    @patch("utils.jenkins_api._get")
    def test_plugin_missing_returns_none(self, mock_get):
        mock_get.side_effect = _raise_http(404)
        assert get_injected_env_vars("foo", 1) is None


# ---------------------------------------------------------------------------
# get_build_history — agent field
# ---------------------------------------------------------------------------


class TestBuildHistoryAgent:
    @patch("utils.jenkins_api._get")
    def test_agent_field_populated(self, mock_get):
        mock_get.return_value = _mock_response({
            "builds": [
                {"number": 10, "result": "SUCCESS", "duration": 1000, "timestamp": 1700000000000, "builtOn": "linux-01"},
            ]
        })
        result = get_build_history("job", 1)
        assert result[0]["agent"] == "linux-01"

    @patch("utils.jenkins_api._get")
    def test_agent_defaults_to_controller(self, mock_get):
        mock_get.return_value = _mock_response({
            "builds": [
                {"number": 10, "result": "SUCCESS", "duration": 1000, "timestamp": 1700000000000, "builtOn": ""},
            ]
        })
        result = get_build_history("job", 1)
        assert result[0]["agent"] == "controller"
