"""Tests for utils.junit_parser — JUnit XML parsing, classification, and blast radius."""

from __future__ import annotations

import textwrap

import pytest

from utils.junit_parser import (
    classify_failures,
    detect_blast_radius,
    parse_junit_xml,
)


# ---------------------------------------------------------------------------
# Fixtures — reusable XML fragments
# ---------------------------------------------------------------------------


MAVEN_SUREFIRE_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <testsuite name="com.example.DbTests" tests="4" failures="2" errors="1" skipped="1" time="12.345">
      <testcase name="testConnection" classname="com.example.DbTests" time="1.200">
        <failure message="expected:&lt;true&gt; but was:&lt;false&gt;"
                 type="junit.framework.AssertionError">
    junit.framework.AssertionError: expected:&lt;true&gt; but was:&lt;false&gt;
        at com.example.DbTests.testConnection(DbTests.java:42)
        </failure>
        <system-out>Connecting to database at jdbc:postgresql://localhost:5432/test</system-out>
      </testcase>
      <testcase name="testQuery" classname="com.example.DbTests" time="0.500">
      </testcase>
      <testcase name="testTransaction" classname="com.example.DbTests" time="5.000">
        <error message="Connection refused"
               type="java.sql.SQLTransientConnectionException">
    java.sql.SQLTransientConnectionException: Connection refused
        at com.example.DbTests.testTransaction(DbTests.java:88)
    Caused by: java.net.ConnectException: Connection refused
        at java.net.PlainSocketImpl.socketConnect(Native Method)
        </error>
      </testcase>
      <testcase name="testMigration" classname="com.example.DbTests" time="0.001">
        <skipped message="Requires PostgreSQL 15+" />
      </testcase>
      <system-err>WARN  HikariPool-1 - Connection pool nearing exhaustion</system-err>
    </testsuite>
""")

PYTEST_JUNIT_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <testsuites>
      <testsuite name="pytest" tests="3" failures="1" errors="0" skipped="0" time="2.100">
        <testcase name="test_add" classname="tests.test_math" time="0.010">
        </testcase>
        <testcase name="test_divide_by_zero" classname="tests.test_math" time="0.020">
          <failure message="ZeroDivisionError: division by zero">
    def test_divide_by_zero():
    &gt;       result = divide(1, 0)
    E       ZeroDivisionError: division by zero
          </failure>
          <system-out>Running divide(1, 0)...</system-out>
        </testcase>
        <testcase name="test_multiply" classname="tests.test_math" time="0.005">
        </testcase>
      </testsuite>
    </testsuites>
""")

BLAST_RADIUS_XML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <testsuite name="com.example.integration.ApiTests" tests="8" failures="6" errors="0" time="45.0">
      <testcase name="testGetUser" classname="com.example.integration.ApiTests" time="5.0">
        <failure message="Connection timeout after 30000ms">timeout</failure>
      </testcase>
      <testcase name="testCreateUser" classname="com.example.integration.ApiTests" time="5.0">
        <failure message="Connection timeout after 30000ms">timeout</failure>
      </testcase>
      <testcase name="testDeleteUser" classname="com.example.integration.ApiTests" time="5.0">
        <failure message="Connection timeout after 30000ms">timeout</failure>
      </testcase>
      <testcase name="testUpdateUser" classname="com.example.integration.ApiTests" time="5.0">
        <failure message="Connection timeout after 30000ms">timeout</failure>
      </testcase>
      <testcase name="testListUsers" classname="com.example.integration.ApiTests" time="5.0">
        <failure message="Connection timeout after 30000ms">timeout</failure>
      </testcase>
      <testcase name="testSearchUsers" classname="com.example.integration.ApiTests" time="5.0">
        <failure message="Connection timeout after 30000ms">timeout</failure>
      </testcase>
      <testcase name="testHealthCheck" classname="com.example.integration.ApiTests" time="0.1">
      </testcase>
      <testcase name="testVersion" classname="com.example.integration.ApiTests" time="0.1">
      </testcase>
      <system-err>ERROR: API gateway at api.internal:8080 is unreachable</system-err>
    </testsuite>
""")


# ---------------------------------------------------------------------------
# parse_junit_xml — basic parsing
# ---------------------------------------------------------------------------


class TestParseJunitXml:
    def test_maven_surefire_format(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        assert suites is not None
        assert len(suites) == 1

        suite = suites[0]
        assert suite["suite_name"] == "com.example.DbTests"
        assert suite["suite_tests"] == 4
        assert suite["suite_failures"] == 2
        assert suite["suite_errors"] == 1
        assert suite["suite_skipped"] == 1
        assert suite["suite_time_s"] == 12.345
        assert "HikariPool" in suite["suite_stderr"]

    def test_pytest_testsuites_wrapper(self):
        suites = parse_junit_xml(PYTEST_JUNIT_XML)
        assert suites is not None
        assert len(suites) == 1
        assert suites[0]["suite_name"] == "pytest"
        assert suites[0]["suite_tests"] == 3

    def test_case_statuses(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        cases = suites[0]["cases"]
        assert len(cases) == 4

        statuses = {c["name"]: c["status"] for c in cases}
        assert statuses["testConnection"] == "failed"
        assert statuses["testQuery"] == "passed"
        assert statuses["testTransaction"] == "errored"
        assert statuses["testMigration"] == "skipped"

    def test_case_kinds(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        cases = suites[0]["cases"]

        kinds = {c["name"]: c["kind"] for c in cases}
        assert kinds["testConnection"] == "assertion"
        assert kinds["testQuery"] == "passed"
        assert kinds["testTransaction"] == "exception"
        assert kinds["testMigration"] == "skipped"

    def test_case_timing(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        cases = suites[0]["cases"]
        times = {c["name"]: c["time_s"] for c in cases}
        assert times["testConnection"] == 1.2
        assert times["testTransaction"] == 5.0

    def test_failure_message_and_detail(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        conn_case = next(c for c in suites[0]["cases"] if c["name"] == "testConnection")
        assert "expected:" in conn_case["message"]
        assert "DbTests.java:42" in conn_case["detail"]

    def test_error_message_and_detail(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        txn_case = next(c for c in suites[0]["cases"] if c["name"] == "testTransaction")
        assert "Connection refused" in txn_case["message"]
        assert "Caused by:" in txn_case["detail"]

    def test_per_case_stdout(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        conn_case = next(c for c in suites[0]["cases"] if c["name"] == "testConnection")
        assert "jdbc:postgresql" in conn_case["stdout"]

    def test_skipped_reason(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        skip_case = next(c for c in suites[0]["cases"] if c["name"] == "testMigration")
        assert "PostgreSQL 15" in skip_case["skip_reason"]

    def test_pytest_failure_detail(self):
        suites = parse_junit_xml(PYTEST_JUNIT_XML)
        fail_case = next(c for c in suites[0]["cases"] if c["name"] == "test_divide_by_zero")
        assert fail_case["status"] == "failed"
        assert fail_case["kind"] == "exception"
        assert "ZeroDivisionError" in fail_case["message"]
        assert "Running divide" in fail_case["stdout"]


# ---------------------------------------------------------------------------
# parse_junit_xml — invalid / edge cases
# ---------------------------------------------------------------------------


class TestParseInvalidXml:
    def test_not_xml(self):
        assert parse_junit_xml("this is not xml at all") is None

    def test_wrong_root_element(self):
        xml = '<checkstyle version="8.0"><file name="Foo.java"></file></checkstyle>'
        assert parse_junit_xml(xml) is None

    def test_pom_xml(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <project xmlns="http://maven.apache.org/POM/4.0.0">
              <groupId>com.example</groupId>
            </project>
        """)
        assert parse_junit_xml(xml) is None

    def test_empty_testsuite(self):
        xml = '<testsuite name="empty" tests="0" failures="0"></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites is not None
        assert len(suites) == 1
        assert suites[0]["cases"] == []

    def test_empty_testsuites(self):
        xml = "<testsuites></testsuites>"
        suites = parse_junit_xml(xml)
        assert suites is not None
        assert len(suites) == 0

    def test_missing_attributes_default_gracefully(self):
        xml = textwrap.dedent("""\
            <testsuite>
              <testcase name="testFoo">
                <failure>something broke</failure>
              </testcase>
            </testsuite>
        """)
        suites = parse_junit_xml(xml)
        assert suites is not None
        suite = suites[0]
        assert suite["suite_name"] == ""
        assert suite["suite_tests"] == 0
        assert suite["suite_time_s"] == 0.0

        case = suite["cases"][0]
        assert case["name"] == "testFoo"
        assert case["class_name"] == ""
        assert case["status"] == "failed"
        assert case["detail"] == "something broke"

    def test_malformed_time_attribute(self):
        xml = '<testsuite name="t" time="not-a-number"><testcase name="x" time="abc"></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites is not None
        assert suites[0]["suite_time_s"] == 0.0
        assert suites[0]["cases"][0]["time_s"] == 0.0


# ---------------------------------------------------------------------------
# classify_failures
# ---------------------------------------------------------------------------


class TestKindInference:
    """Verify that kind classification uses type attr and message heuristics."""

    def test_assertion_type_attr(self):
        xml = '<testsuite name="t" tests="1"><testcase name="a" classname="X"><failure type="junit.framework.AssertionError" message="expected 1 got 2">stack</failure></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites[0]["cases"][0]["kind"] == "assertion"

    def test_exception_type_attr(self):
        xml = '<testsuite name="t" tests="1"><testcase name="a" classname="X"><failure type="java.lang.NullPointerException" message="null">stack</failure></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites[0]["cases"][0]["kind"] == "exception"

    def test_exception_in_message_no_type(self):
        xml = '<testsuite name="t" tests="1"><testcase name="a" classname="X"><failure message="ConnectionRefusedException: port 5432">stack</failure></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites[0]["cases"][0]["kind"] == "exception"

    def test_timeout_in_message(self):
        xml = '<testsuite name="t" tests="1"><testcase name="a" classname="X"><failure message="Connection timeout after 30s">stack</failure></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites[0]["cases"][0]["kind"] == "exception"

    def test_fallback_failure_tag_no_signals(self):
        """No type attr and no exception keywords in message → defaults to assertion."""
        xml = '<testsuite name="t" tests="1"><testcase name="a" classname="X"><failure message="expected true but got false">stack</failure></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites[0]["cases"][0]["kind"] == "assertion"

    def test_fallback_error_tag_no_signals(self):
        """<error> with no keywords → defaults to exception."""
        xml = '<testsuite name="t" tests="1"><testcase name="a" classname="X"><error message="something happened">stack</error></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites[0]["cases"][0]["kind"] == "exception"

    def test_type_attr_overrides_element_tag(self):
        """<failure> with an Exception type attr → exception, not assertion."""
        xml = '<testsuite name="t" tests="1"><testcase name="a" classname="X"><failure type="java.io.IOException" message="file not found">stack</failure></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites[0]["cases"][0]["kind"] == "exception"

    def test_bare_error_word_not_classified_as_exception(self):
        """Message containing bare 'Error' (not prefixed like SomeError) should
        fall through to assertion for <failure> elements."""
        xml = '<testsuite name="t" tests="1"><testcase name="a" classname="X"><failure message="Error count should be 0">stack</failure></testcase></testsuite>'
        suites = parse_junit_xml(xml)
        assert suites[0]["cases"][0]["kind"] == "assertion"


class TestClassifyFailures:
    def test_mixed_failures(self):
        suites = parse_junit_xml(MAVEN_SUREFIRE_XML)
        result = classify_failures(suites)
        assert result["assertions"] == 1
        assert result["exceptions"] == 1
        assert result["total_failed"] == 2

    def test_all_exceptions_by_message(self):
        """BLAST_RADIUS_XML failures have message 'Connection timeout...' —
        the heuristic detects 'Timeout' and classifies as exception."""
        suites = parse_junit_xml(BLAST_RADIUS_XML)
        result = classify_failures(suites)
        assert result["assertions"] == 0
        assert result["exceptions"] == 6
        assert result["total_failed"] == 6

    def test_no_failures(self):
        xml = textwrap.dedent("""\
            <testsuite name="clean" tests="2">
              <testcase name="a" classname="X" time="0.1"></testcase>
              <testcase name="b" classname="X" time="0.2"></testcase>
            </testsuite>
        """)
        suites = parse_junit_xml(xml)
        result = classify_failures(suites)
        assert result["total_failed"] == 0


# ---------------------------------------------------------------------------
# detect_blast_radius
# ---------------------------------------------------------------------------


class TestDetectBlastRadius:
    def test_detects_blast_radius(self):
        suites = parse_junit_xml(BLAST_RADIUS_XML)
        blasts = detect_blast_radius(suites)
        assert len(blasts) == 1

        blast = blasts[0]
        assert blast["suite_name"] == "com.example.integration.ApiTests"
        assert blast["shared_count"] == 6
        assert blast["total_failures"] == 6
        assert "timeout" in blast["shared_message"].lower()
        assert len(blast["affected_tests"]) == 6
        assert "testGetUser" in blast["affected_tests"]
        assert "api.internal:8080" in blast["suite_stderr"]

    def test_no_blast_when_errors_differ(self):
        xml = textwrap.dedent("""\
            <testsuite name="varied" tests="4" failures="4">
              <testcase name="a" classname="X"><failure message="error type A">A</failure></testcase>
              <testcase name="b" classname="X"><failure message="error type B">B</failure></testcase>
              <testcase name="c" classname="X"><failure message="error type C">C</failure></testcase>
              <testcase name="d" classname="X"><failure message="error type D">D</failure></testcase>
            </testsuite>
        """)
        suites = parse_junit_xml(xml)
        blasts = detect_blast_radius(suites)
        assert len(blasts) == 0

    def test_no_blast_when_fewer_than_three_failures(self):
        xml = textwrap.dedent("""\
            <testsuite name="small" tests="2" failures="2">
              <testcase name="a" classname="X"><failure message="same error">e</failure></testcase>
              <testcase name="b" classname="X"><failure message="same error">e</failure></testcase>
            </testsuite>
        """)
        suites = parse_junit_xml(xml)
        blasts = detect_blast_radius(suites)
        assert len(blasts) == 0

    def test_blast_with_mixed_errors_above_threshold(self):
        xml = textwrap.dedent("""\
            <testsuite name="mixed" tests="5" failures="5">
              <testcase name="a" classname="X"><failure message="timeout">t</failure></testcase>
              <testcase name="b" classname="X"><failure message="timeout">t</failure></testcase>
              <testcase name="c" classname="X"><failure message="timeout">t</failure></testcase>
              <testcase name="d" classname="X"><failure message="timeout">t</failure></testcase>
              <testcase name="e" classname="X"><failure message="different error">d</failure></testcase>
            </testsuite>
        """)
        suites = parse_junit_xml(xml)
        blasts = detect_blast_radius(suites)
        assert len(blasts) == 1
        assert blasts[0]["shared_count"] == 4

    def test_no_blast_for_passing_suites(self):
        xml = textwrap.dedent("""\
            <testsuite name="green" tests="5">
              <testcase name="a" classname="X"></testcase>
              <testcase name="b" classname="X"></testcase>
              <testcase name="c" classname="X"></testcase>
              <testcase name="d" classname="X"></testcase>
              <testcase name="e" classname="X"></testcase>
            </testsuite>
        """)
        suites = parse_junit_xml(xml)
        blasts = detect_blast_radius(suites)
        assert len(blasts) == 0

    def test_custom_threshold(self):
        xml = textwrap.dedent("""\
            <testsuite name="t" tests="10" failures="10">
              <testcase name="a" classname="X"><failure message="err1">e</failure></testcase>
              <testcase name="b" classname="X"><failure message="err1">e</failure></testcase>
              <testcase name="c" classname="X"><failure message="err1">e</failure></testcase>
              <testcase name="d" classname="X"><failure message="err2">e</failure></testcase>
              <testcase name="e" classname="X"><failure message="err2">e</failure></testcase>
              <testcase name="f" classname="X"><failure message="err2">e</failure></testcase>
              <testcase name="g" classname="X"><failure message="err3">e</failure></testcase>
              <testcase name="h" classname="X"><failure message="err3">e</failure></testcase>
              <testcase name="i" classname="X"><failure message="err3">e</failure></testcase>
              <testcase name="j" classname="X"><failure message="err4">e</failure></testcase>
            </testsuite>
        """)
        suites = parse_junit_xml(xml)

        # Default 60% threshold: no single group dominates
        blasts_default = detect_blast_radius(suites, threshold=0.6)
        assert len(blasts_default) == 0

        # Lower 25% threshold: multiple groups qualify
        blasts_low = detect_blast_radius(suites, threshold=0.25)
        assert len(blasts_low) == 3


# ---------------------------------------------------------------------------
# Multiple suites in one file (pytest / aggregate reports)
# ---------------------------------------------------------------------------


class TestMultipleSuites:
    def test_testsuites_with_multiple_children(self):
        xml = textwrap.dedent("""\
            <?xml version="1.0"?>
            <testsuites>
              <testsuite name="unit" tests="2" failures="0">
                <testcase name="test_a" classname="tests.unit" time="0.01"></testcase>
                <testcase name="test_b" classname="tests.unit" time="0.02"></testcase>
              </testsuite>
              <testsuite name="integration" tests="2" failures="1">
                <testcase name="test_c" classname="tests.integration" time="1.0">
                  <failure message="service down">timeout</failure>
                </testcase>
                <testcase name="test_d" classname="tests.integration" time="0.5"></testcase>
              </testsuite>
            </testsuites>
        """)
        suites = parse_junit_xml(xml)
        assert len(suites) == 2
        assert suites[0]["suite_name"] == "unit"
        assert suites[1]["suite_name"] == "integration"

        result = classify_failures(suites)
        assert result["assertions"] == 1
        assert result["total_failed"] == 1
