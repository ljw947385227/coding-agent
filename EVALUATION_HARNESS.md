# KamaClaude Agent Evaluator

The Evaluator runs coding-agent tasks from a fixed Git commit in detached worktrees, executes a
trusted external oracle, preserves evidence, and optionally scores tool-selection behavior.
It evaluates the production `AgentHarness`; it is not the Agent runtime harness itself.

## Validate and run a suite

Validation does not call a model or create a worktree:

```powershell
uv run kama eval examples/sandbox_diagnosis_suite.example.json --validate-only
```

Run the suite with the configured model provider:

```powershell
$env:ANTHROPIC_API_KEY = "..."
uv run kama eval examples/sandbox_diagnosis_suite.example.json `
  --output "$HOME/.kama/evaluations/sandbox-diagnosis" `
  --repeats 3
```

The command exits non-zero when any run fails its oracle or behavior expectations, so it can be
used as a CI quality gate.

## Sandbox scenarios

Each task may override the execution environment used by the Agent:

```json
{
  "sandbox": {
    "backend": "docker",
    "image": "kama-sandbox-python:3.12-v1",
    "network": "none",
    "memory_mb": 512,
    "cpus": 1.0,
    "pids_limit": 64,
    "tmpfs_mb": 256
  },
  "oracle_backend": "local"
}
```

`oracle_backend=local` is the default and keeps the evaluator independent from the environment
under test. Use `sandbox` only when the oracle itself must execute in the same image.

## Behavior expectations

The Evaluator can verify that an Agent distinguished an environment problem from an application
problem instead of merely checking the final patch:

```json
{
  "expectations": {
    "sandbox_info": "required",
    "max_sandbox_info_calls": 1,
    "allow_source_changes": false,
    "required_tools": ["bash", "sandbox_info"],
    "forbidden_tools": [],
    "answer_patterns": ["sandbox|environment", "pytest", "rebuild"]
  }
}
```

The included suite contains one missing-dependency scenario and one ordinary-assertion control
case. Together they measure both environment-detection recall and unnecessary-inspection risk.

## Artifacts

Each run writes an immutable artifact directory containing:

- `request.json`: resolved task contract;
- `runs/<run_id>/events.jsonl`: complete runtime event stream;
- `tool_trace.json`: stable tool-decision trace without tool output payloads;
- `agent.patch` or `agent.patch.truncated`: isolated repository change;
- `verification.json`: external oracle result;
- `score.json`: behavior expectation result when configured;
- `metrics.json`: steps, tokens, failures, per-tool counts and `sandbox_info` calls;
- `result.json`: final run status and references to all evidence.

The suite-level `summary.json` includes success, oracle and behavior pass rates, duration p50/p95,
token totals, tool failures and total `sandbox_info` calls. No benchmark number should be reported
until a live-model suite has actually produced these artifacts.
