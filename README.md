# Jenkins Investigator MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io/) server that helps AI assistants debug failing Jenkins CI jobs efficiently — without blowing through a context window.

> **Note:** This codebase was generated with AI assistance (Claude/Cursor) and reviewed, tested, and validated by humans.

## How It Works

Raw Jenkins API responses are enormous. This server acts as a filter: it fetches the verbose data, strips the noise, and returns only what matters — error snippets, commit messages, failing tests.

## Tools

### Bundles — Start Here

Bundles combine multiple API calls into a single tool invocation, saving tokens and latency.

| Bundle | What It Does |
|--------|-------------|
| `investigate_build_failure` | **Primary entry point.** Returns build info, stages, errors, tests, commits, params, and trend in one call. |
| `compare_failing_vs_passing` | Diffs the last failing vs last passing build: parameter changes, agent, trigger, cumulative commits. |
| `deep_dive_test_failures` | Traces each failing test back through recent builds to find the regression point and suspect commit. Enriches with JUnit XML artifacts when available (failure classification, blast-radius detection, extended stdout/stderr). |
| `analyze_flaky_job` | Scores flakiness and clusters failures by node, stage, and time of day. |
| `diagnose_infrastructure_issue` | Checks node health and per-node failure correlation to determine if a failure is infra-related. |
| `search_across_jobs` | Searches console logs across all jobs in a folder for a specific error pattern. Concurrent, with status filtering and early termination. |
| `triage_folder` | Scans a folder for broken jobs with consecutive-failure counts — a team health dashboard. |

### Individual Tools

Use these for targeted follow-up after a bundle gives you the big picture.

| Tool | When to Use |
|------|-------------|
| `get_last_build_info` | Discover the latest (or latest failed) build number from a job name. |
| `list_jobs` | Browse jobs in a Jenkins folder. |
| `get_build_summary` | Quick build overview: status, runtime, agent, trigger. |
| `get_pipeline_stages` | Stage table with status and duration. |
| `get_build_parameters` | See build parameters (branch, env, flags). |
| `get_build_history` | Recent build results with trend analysis. |
| `get_scm_changes` | Commits in this build (Git, SVN, Mercurial). |
| `get_error_logs` | Prioritized error extract from the console log (≤250 lines). |
| `get_stage_logs` | Error extract from a specific pipeline stage's log. |
| `search_console_log` | Grep the console log for a string or regex with context lines. |
| `get_test_failures` | Failing tests with error messages and stack traces. |
| `get_build_artifacts` | List artifacts or fetch a specific artifact's content. |
| `get_job_config` | Parsed job config: SCM, triggers, agent label, pipeline definition. |
| `get_build_environment` | CI-relevant environment variables (requires EnvInject plugin). |
| `get_queue_info` | Build queue with reasons why builds are waiting. |
| `compare_builds` | Diff any two builds (params, agent, trigger, commits). |
| `get_upstream_downstream_builds` | Upstream trigger chain for a build. |
| `get_node_status` | Agent online status and disk space. |
| `get_node_list` | All agents with labels, executors, and disk space. |
| `get_pipeline_flow_nodes` | Parallel branches and step-level detail inside a stage. |

## Setup

### 1. Install dependencies

```bash
pip install uv
uv pip install -e .
```

Or with plain pip:

```bash
pip install fastmcp>=3.0.0 requests>=2.31 pydantic>=2.0 python-dotenv>=1.0
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env`:

```
JENKINS_URL=http://your-jenkins:8080
JENKINS_USER=your-username
JENKINS_TOKEN=your-api-token
```

Your API token can be generated from Jenkins → your user → Configure → API Token.

### 3. Run the server

```bash
python server.py
```

By default the server starts with Streamable HTTP transport on `0.0.0.0:8000`. You'll see the local and network URLs printed on startup.

### 4. Connect from Cursor / Claude Desktop

**HTTP transport (default):**

```json
{
  "mcpServers": {
    "jenkins-investigator": {
      "url": "http://192.168.1.42:8000/mcp"
    }
  }
}
```

Replace the IP with the Network URL printed at server startup.

**Stdio transport** (set `MCP_TRANSPORT=stdio` in `.env`):

```json
{
  "mcpServers": {
    "jenkins-investigator": {
      "command": "python",
      "args": ["/absolute/path/to/jenkins-mcp/server.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "JENKINS_URL": "http://your-jenkins:8080",
        "JENKINS_USER": "your-username",
        "JENKINS_TOKEN": "your-api-token"
      }
    }
  }
}
```

## Job Names

For simple jobs, use the job name directly: `my-job`.

For jobs inside folders or multibranch pipelines, use slash-separated paths:

```
my-org/my-repo/main
```

The server automatically converts this to the correct Jenkins API path (`/job/my-org/job/my-repo/job/main/`).

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `JENKINS_URL` | Yes | — | Base URL of your Jenkins instance |
| `JENKINS_USER` | Yes | — | Jenkins username |
| `JENKINS_TOKEN` | Yes | — | Jenkins API token (not your password) |
| `JENKINS_VERIFY_SSL` | No | `true` | Set to `false` for Jenkins with self-signed or internal CA certificates |
| `MCP_TRANSPORT` | No | `http` | Transport protocol: `http` (Streamable HTTP) or `stdio` |
| `MCP_HOST` | No | `0.0.0.0` | Bind address for HTTP transport |
| `MCP_PORT` | No | `8000` | Port for HTTP transport |
| `TOOL_DELAY_SECONDS` | No | `2` | Delay between tool calls to avoid hitting AI provider TPM limits |

## Documentation

See [Architecture](docs/architecture.md) for system design, layer responsibilities, and resilience strategy.

See [Tool Flow](docs/tool-flow.md) for decision trees, bundle internals, and per-tool quick reference.

## Project Structure

```
jenkins-mcp/
├── server.py          # FastMCP server — 20 individual tools + 7 bundles
├── utils/
│   ├── __init__.py
│   ├── jenkins_api.py # Jenkins REST wrappers with retry/backoff
│   ├── junit_parser.py# JUnit XML parser (assertion vs exception classification, blast radius)
│   ├── log_parser.py  # Priority-budgeted error extraction
│   └── scm.py         # changeSet/changeSets normalization across SCMs
├── docs/
│   ├── architecture.md # System design and layer diagrams
│   └── tool-flow.md    # Decision trees and bundle internals
├── tests/             # pytest suite (162 tests)
├── .env               # Your credentials (gitignored)
├── .env.example       # Template
├── .gitignore
└── pyproject.toml
```

## Security

- Credentials are read from environment variables only — never hardcoded.
- The server is **read-only**: no tools delete jobs, wipe workspaces, or trigger builds.
- The `.env` file is gitignored by default.
- Console logs are capped at 10 MB to prevent memory exhaustion on large builds.
