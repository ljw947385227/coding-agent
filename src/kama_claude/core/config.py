from __future__ import annotations

import ast
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import load_dotenv

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.kama/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.kama/config.toml"
_DEFAULT_MAX_STEPS = 20
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_TRACE_FILE = "~/.kama/traces/daemon.jsonl"


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS


@dataclass
class LlmConfig:
    default_model: str = _DEFAULT_MODEL
    router: str = "static"  # "static" | "rule_based" (S4) | "cost_budget" (S6)


@dataclass
class TraceConfig:
    enabled: bool = True
    file: str = _DEFAULT_TRACE_FILE
    include_llm_payload: bool = True  # false 时 LLM 记录只保留摘要


@dataclass
class PermissionConfig:
    timeout_s: float = 0  # 审批超时秒数；0 表示不超时


@dataclass
class CompactionConfig:
    # context_pct 触发自动压缩的阈值（0 表示禁用，推荐手动 /compact）
    auto_threshold: float = 0.0
    tool_result_limit: int = 8_000  # tool_result 截断触发字符数
    tool_result_keep: int = 4_000  # 截断后保留的前缀字符数


@dataclass
class VerificationConfig:
    mode: Literal["off", "suggest", "required_on_write"] = "required_on_write"
    max_attempts: int = 3
    max_total_seconds: float = 600.0


@dataclass
class DockerExecutionConfig:
    cli: str = "docker"
    image: str = "kama-sandbox-python:3.12-v1"
    network: Literal["none", "bridge"] = "none"
    memory_mb: int = 512
    cpus: float = 1.0
    pids_limit: int = 64
    tmpfs_mb: int = 256
    user: str = "10001:10001"


@dataclass
class ExecutionConfig:
    backend: Literal["local", "docker"] = "local"
    docker: DockerExecutionConfig = field(default_factory=DockerExecutionConfig)


@dataclass
class RunConfig:
    slow_run_seconds: float = 600.0
    stalled_seconds: float = 60.0
    tool_warning_seconds: float = 90.0
    warning_snooze_seconds: float = 300.0
    monitor_interval_seconds: float = 2.0


@dataclass
class FileAccessConfig:
    ignore_files: list[str] = field(default_factory=list)


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"  # "stdio" | "tcp"
    command: str = ""  # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    host: str = "localhost"  # tcp 专用
    port: int = 3000  # tcp 专用


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)


@dataclass
class KamaConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    runs: RunConfig = field(default_factory=RunConfig)
    files: FileAccessConfig = field(default_factory=FileAccessConfig)
    mcp: McpConfig = field(default_factory=McpConfig)


# 构建并返回运行时配置：默认值 → 全局 TOML → 项目本地 TOML → .env → 系统环境变量（后者优先级最高）
def get_config() -> KamaConfig:
    config = KamaConfig()

    # .env 必须在读取 KAMA_CONFIG 之前加载，以便 .env 中的 KAMA_CONFIG 能影响 TOML 路径
    load_dotenv(".env", override=False)

    # 若显式指定 KAMA_CONFIG，只读该文件；否则按优先级叠加：全局 → 项目本地
    explicit = os.environ.get("KAMA_CONFIG")
    if explicit:
        config_paths = [Path(explicit).expanduser()]
    else:
        config_paths = [
            Path(_DEFAULT_CONFIG_PATH).expanduser(),
            Path(".kama/config.toml"),
        ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"Config parse error ({config_path}): {e}") from e
            _apply_toml(config, data)

    _apply_env(config)
    return config


# 将已解析的 TOML 根表写入 config；未知小节或类型错误时退出进程
def _apply_toml(config: KamaConfig, data: dict[str, Any]) -> None:
    known_sections = {
        "core",
        "logging",
        "agent",
        "llm",
        "trace",
        "permission",
        "compaction",
        "verification",
        "execution",
        "runs",
        "files",
        "mcp",
    }
    unknown = set(data.keys()) - known_sections
    if unknown:
        raise SystemExit(f"Unknown top-level config keys: {', '.join(sorted(unknown))}")

    if "core" in data:
        core = data["core"]
        if not isinstance(core, dict):
            raise SystemExit("Config error: [core] must be a table")
        unknown_core: set[str] = set(core.keys()) - {"host", "port"}
        if unknown_core:
            raise SystemExit(f"Unknown [core] keys: {', '.join(sorted(unknown_core))}")
        if "host" in core:
            val = core["host"]
            if not isinstance(val, str):
                raise SystemExit("Config error: core.host must be a string")
            config.host = val
        if "port" in core:
            val = core["port"]
            if not isinstance(val, int):
                raise SystemExit("Config error: core.port must be an integer")
            config.port = val

    if "logging" in data:
        log = data["logging"]
        if not isinstance(log, dict):
            raise SystemExit("Config error: [logging] must be a table")
        unknown_log: set[str] = set(log.keys()) - {"level", "file", "format"}
        if unknown_log:
            raise SystemExit(f"Unknown [logging] keys: {', '.join(sorted(unknown_log))}")
        for key in ("level", "file", "format"):
            if key in log:
                val = log[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: logging.{key} must be a string")
                setattr(config.logging, key, val)

    if "agent" in data:
        agent = data["agent"]
        if not isinstance(agent, dict):
            raise SystemExit("Config error: [agent] must be a table")
        unknown_agent: set[str] = set(agent.keys()) - {"max_steps"}
        if unknown_agent:
            raise SystemExit(f"Unknown [agent] keys: {', '.join(sorted(unknown_agent))}")
        if "max_steps" in agent:
            val = agent["max_steps"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: agent.max_steps must be a positive integer")
            config.agent.max_steps = val

    if "llm" in data:
        llm = data["llm"]
        if not isinstance(llm, dict):
            raise SystemExit("Config error: [llm] must be a table")
        unknown_llm: set[str] = set(llm.keys()) - {"default_model", "router"}
        if unknown_llm:
            raise SystemExit(f"Unknown [llm] keys: {', '.join(sorted(unknown_llm))}")
        if "default_model" in llm:
            val = llm["default_model"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.default_model must be a string")
            config.llm.default_model = val
        if "router" in llm:
            val = llm["router"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router must be a string")
            config.llm.router = val

    if "trace" in data:
        trace = data["trace"]
        if not isinstance(trace, dict):
            raise SystemExit("Config error: [trace] must be a table")
        unknown_trace: set[str] = set(trace.keys()) - {"enabled", "file", "include_llm_payload"}
        if unknown_trace:
            raise SystemExit(f"Unknown [trace] keys: {', '.join(sorted(unknown_trace))}")
        if "enabled" in trace:
            val = trace["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.enabled must be a boolean")
            config.trace.enabled = val
        if "file" in trace:
            val = trace["file"]
            if not isinstance(val, str):
                raise SystemExit("Config error: trace.file must be a string")
            config.trace.file = val
        if "include_llm_payload" in trace:
            val = trace["include_llm_payload"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.include_llm_payload must be a boolean")
            config.trace.include_llm_payload = val

    if "permission" in data:
        perm = data["permission"]
        if not isinstance(perm, dict):
            raise SystemExit("Config error: [permission] must be a table")
        unknown_perm: set[str] = set(perm.keys()) - {"timeout_s"}
        if unknown_perm:
            raise SystemExit(f"Unknown [permission] keys: {', '.join(sorted(unknown_perm))}")
        if "timeout_s" in perm:
            val = perm["timeout_s"]
            if not isinstance(val, (int, float)) or val < 0:
                raise SystemExit("Config error: permission.timeout_s must be a non-negative number")
            config.permission.timeout_s = float(val)

    if "compaction" in data:
        comp = data["compaction"]
        if not isinstance(comp, dict):
            raise SystemExit("Config error: [compaction] must be a table")
        unknown_comp: set[str] = set(comp.keys()) - {
            "auto_threshold",
            "tool_result_limit",
            "tool_result_keep",
        }
        if unknown_comp:
            raise SystemExit(f"Unknown [compaction] keys: {', '.join(sorted(unknown_comp))}")
        if "auto_threshold" in comp:
            val = comp["auto_threshold"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise SystemExit("Config error: compaction.auto_threshold must be between 0 and 1")
            config.compaction.auto_threshold = float(val)
        if "tool_result_limit" in comp:
            val = comp["tool_result_limit"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_limit must be a positive integer"
                )
            config.compaction.tool_result_limit = val
        if "tool_result_keep" in comp:
            val = comp["tool_result_keep"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_keep must be a positive integer"
                )
            config.compaction.tool_result_keep = val

    if "verification" in data:
        verification = data["verification"]
        if not isinstance(verification, dict):
            raise SystemExit("Config error: [verification] must be a table")
        unknown_verification: set[str] = set(verification.keys()) - {
            "mode",
            "max_attempts",
            "max_total_seconds",
        }
        if unknown_verification:
            raise SystemExit(
                "Unknown [verification] keys: " + ", ".join(sorted(unknown_verification))
            )
        if "mode" in verification:
            mode = verification["mode"]
            if mode not in ("off", "suggest", "required_on_write"):
                raise SystemExit(
                    "Config error: verification.mode must be off, suggest, or required_on_write"
                )
            config.verification.mode = mode
        if "max_attempts" in verification:
            max_attempts = verification["max_attempts"]
            if not isinstance(max_attempts, int) or max_attempts <= 0:
                raise SystemExit(
                    "Config error: verification.max_attempts must be a positive integer"
                )
            config.verification.max_attempts = max_attempts
        if "max_total_seconds" in verification:
            max_total_seconds = verification["max_total_seconds"]
            if not isinstance(max_total_seconds, (int, float)) or max_total_seconds <= 0:
                raise SystemExit(
                    "Config error: verification.max_total_seconds must be a positive number"
                )
            config.verification.max_total_seconds = float(max_total_seconds)

    if "execution" in data:
        execution = data["execution"]
        if not isinstance(execution, dict):
            raise SystemExit("Config error: [execution] must be a table")
        unknown_execution = set(execution.keys()) - {"backend", "docker"}
        if unknown_execution:
            raise SystemExit(
                f"Unknown [execution] keys: {', '.join(sorted(unknown_execution))}"
            )
        if "backend" in execution:
            backend = execution["backend"]
            if backend not in ("local", "docker"):
                raise SystemExit("Config error: execution.backend must be local or docker")
            config.execution.backend = backend
        if "docker" in execution:
            _apply_docker_toml(config.execution.docker, execution["docker"])

    if "runs" in data:
        runs = data["runs"]
        if not isinstance(runs, dict):
            raise SystemExit("Config error: [runs] must be a table")
        allowed_run_keys = {
            "slow_run_seconds",
            "stalled_seconds",
            "tool_warning_seconds",
            "warning_snooze_seconds",
            "monitor_interval_seconds",
        }
        unknown_runs = set(runs.keys()) - allowed_run_keys
        if unknown_runs:
            raise SystemExit(f"Unknown [runs] keys: {', '.join(sorted(unknown_runs))}")
        for key in allowed_run_keys:
            if key not in runs:
                continue
            value = runs[key]
            if not isinstance(value, (int, float)) or value <= 0:
                raise SystemExit(f"Config error: runs.{key} must be a positive number")
            setattr(config.runs, key, float(value))

    if "files" in data:
        files = data["files"]
        if not isinstance(files, dict):
            raise SystemExit("Config error: [files] must be a table")
        unknown_files: set[str] = set(files.keys()) - {"ignore_file"}
        if unknown_files:
            raise SystemExit(f"Unknown [files] keys: {', '.join(sorted(unknown_files))}")
        if "ignore_file" in files:
            value = files["ignore_file"]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise SystemExit("Config error: files.ignore_file must be an array of strings")
            config.files.ignore_files = _merge_ignore_files(config.files.ignore_files, value)

    if "mcp" in data:
        mcp = data["mcp"]
        if not isinstance(mcp, dict):
            raise SystemExit("Config error: [mcp] must be a table")
        unknown_mcp: set[str] = set(mcp.keys()) - {"servers"}
        if unknown_mcp:
            raise SystemExit(f"Unknown [mcp] keys: {', '.join(sorted(unknown_mcp))}")
        servers_raw = mcp.get("servers", [])
        if not isinstance(servers_raw, list):
            raise SystemExit("Config error: mcp.servers must be an array of tables")
        for i, srv in enumerate(servers_raw):
            if not isinstance(srv, dict):
                raise SystemExit(f"Config error: mcp.servers[{i}] must be a table")
            name = srv.get("name")
            if not isinstance(name, str) or not name:
                raise SystemExit(f"Config error: mcp.servers[{i}].name must be a non-empty string")
            transport = srv.get("transport", "stdio")
            if transport not in ("stdio", "tcp"):
                raise SystemExit(
                    f"Config error: mcp.servers[{i}].transport must be 'stdio' or 'tcp'"
                )
            s = McpServerConfig(name=name, transport=transport)
            if "command" in srv:
                val = srv["command"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].command must be a string")
                s.command = val
            if "args" in srv:
                val = srv["args"]
                if not isinstance(val, list):
                    raise SystemExit(f"Config error: mcp.servers[{i}].args must be an array")
                s.args = [str(a) for a in val]
            if "env" in srv:
                val = srv["env"]
                if not isinstance(val, dict):
                    raise SystemExit(f"Config error: mcp.servers[{i}].env must be a table")
                s.env = {str(k): str(v) for k, v in val.items()}
            if "host" in srv:
                val = srv["host"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].host must be a string")
                s.host = val
            if "port" in srv:
                val = srv["port"]
                if not isinstance(val, int):
                    raise SystemExit(f"Config error: mcp.servers[{i}].port must be an integer")
                s.port = val
            config.mcp.servers.append(s)


# 用 KAMA_* 环境变量覆盖 config 中对应字段（若变量已设置）
def _apply_env(config: KamaConfig) -> None:
    host = os.environ.get("KAMA_HOST")
    if host is not None:
        config.host = host

    port_str = os.environ.get("KAMA_PORT")
    if port_str is not None:
        try:
            config.port = int(port_str)
        except ValueError:
            raise SystemExit(f"Config error: KAMA_PORT must be an integer, got: {port_str!r}")

    log_level = os.environ.get("KAMA_LOG_LEVEL")
    if log_level is not None:
        config.logging.level = log_level

    log_file = os.environ.get("KAMA_LOG_FILE")
    if log_file is not None:
        config.logging.file = log_file

    log_format = os.environ.get("KAMA_LOG_FORMAT")
    if log_format is not None:
        config.logging.format = log_format

    max_steps_str = os.environ.get("KAMA_MAX_STEPS")
    if max_steps_str is not None:
        try:
            val = int(max_steps_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: KAMA_MAX_STEPS must be a positive integer,"
                    f" got: {max_steps_str!r}"
                )
            config.agent.max_steps = val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_MAX_STEPS must be an integer, got: {max_steps_str!r}"
            )

    default_model = os.environ.get("KAMA_LLM_DEFAULT_MODEL")
    if default_model is not None:
        config.llm.default_model = default_model

    trace_enabled = os.environ.get("KAMA_TRACE_ENABLED")
    if trace_enabled is not None:
        config.trace.enabled = trace_enabled.lower() not in ("0", "false", "no")

    trace_file = os.environ.get("KAMA_TRACE_FILE")
    if trace_file is not None:
        config.trace.file = trace_file

    trace_payload = os.environ.get("KAMA_TRACE_INCLUDE_LLM_PAYLOAD")
    if trace_payload is not None:
        config.trace.include_llm_payload = trace_payload.lower() not in ("0", "false", "no")

    perm_timeout = os.environ.get("KAMA_PERMISSION_TIMEOUT_S")
    if perm_timeout is not None:
        try:
            perm_timeout_val = float(perm_timeout)
            if perm_timeout_val < 0:
                raise SystemExit(
                    f"Config error: KAMA_PERMISSION_TIMEOUT_S must be >= 0, got: {perm_timeout!r}"
                )
            config.permission.timeout_s = perm_timeout_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_PERMISSION_TIMEOUT_S must be a number, got: {perm_timeout!r}"
            )

    compact_threshold = os.environ.get("KAMA_COMPACT_THRESHOLD")
    if compact_threshold is not None:
        try:
            compact_threshold_val = float(compact_threshold)
            if not (0.0 <= compact_threshold_val <= 1.0):
                raise SystemExit(
                    "Config error: KAMA_COMPACT_THRESHOLD must be between 0 and 1,"
                    f" got: {compact_threshold!r}"
                )
            config.compaction.auto_threshold = compact_threshold_val
        except ValueError:
            raise SystemExit(
                f"Config error: KAMA_COMPACT_THRESHOLD must be a number, got: {compact_threshold!r}"
            )

    compact_tool_limit = os.environ.get("KAMA_COMPACT_TOOL_LIMIT")
    if compact_tool_limit is not None:
        try:
            compact_tool_limit_val = int(compact_tool_limit)
            if compact_tool_limit_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_COMPACT_TOOL_LIMIT must be a positive integer,"
                    f" got: {compact_tool_limit!r}"
                )
            config.compaction.tool_result_limit = compact_tool_limit_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_COMPACT_TOOL_LIMIT must be an integer,"
                f" got: {compact_tool_limit!r}"
            )

    compact_tool_keep = os.environ.get("KAMA_COMPACT_TOOL_KEEP")
    if compact_tool_keep is not None:
        try:
            compact_tool_keep_val = int(compact_tool_keep)
            if compact_tool_keep_val <= 0:
                raise SystemExit(
                    "Config error: KAMA_COMPACT_TOOL_KEEP must be a positive integer,"
                    f" got: {compact_tool_keep!r}"
                )
            config.compaction.tool_result_keep = compact_tool_keep_val
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_COMPACT_TOOL_KEEP must be an integer,"
                f" got: {compact_tool_keep!r}"
            )

    verification_mode = os.environ.get("KAMA_VERIFICATION_MODE")
    if verification_mode is not None:
        if verification_mode not in ("off", "suggest", "required_on_write"):
            raise SystemExit(
                "Config error: KAMA_VERIFICATION_MODE must be off, suggest, or required_on_write"
            )
        config.verification.mode = cast(
            Literal["off", "suggest", "required_on_write"],
            verification_mode,
        )

    verification_attempts = os.environ.get("KAMA_VERIFICATION_MAX_ATTEMPTS")
    if verification_attempts is not None:
        try:
            attempts = int(verification_attempts)
            if attempts <= 0:
                raise ValueError
            config.verification.max_attempts = attempts
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_VERIFICATION_MAX_ATTEMPTS must be a positive integer"
            )

    verification_seconds = os.environ.get("KAMA_VERIFICATION_MAX_TOTAL_SECONDS")
    if verification_seconds is not None:
        try:
            seconds = float(verification_seconds)
            if seconds <= 0:
                raise ValueError
            config.verification.max_total_seconds = seconds
        except ValueError:
            raise SystemExit(
                "Config error: KAMA_VERIFICATION_MAX_TOTAL_SECONDS must be a positive number"
            )

    execution_backend = os.environ.get("KAMA_EXECUTION_BACKEND")
    if execution_backend is not None:
        if execution_backend not in ("local", "docker"):
            raise SystemExit("Config error: KAMA_EXECUTION_BACKEND must be local or docker")
        config.execution.backend = cast(Literal["local", "docker"], execution_backend)

    docker_string_env = {
        "KAMA_DOCKER_CLI": "cli",
        "KAMA_DOCKER_IMAGE": "image",
        "KAMA_DOCKER_USER": "user",
    }
    for env_name, field_name in docker_string_env.items():
        raw = os.environ.get(env_name)
        if raw is not None:
            if not raw:
                raise SystemExit(f"Config error: {env_name} must not be empty")
            setattr(config.execution.docker, field_name, raw)

    docker_network = os.environ.get("KAMA_DOCKER_NETWORK")
    if docker_network is not None:
        if docker_network not in ("none", "bridge"):
            raise SystemExit("Config error: KAMA_DOCKER_NETWORK must be none or bridge")
        config.execution.docker.network = cast(Literal["none", "bridge"], docker_network)

    docker_int_env = {
        "KAMA_DOCKER_MEMORY_MB": "memory_mb",
        "KAMA_DOCKER_PIDS_LIMIT": "pids_limit",
        "KAMA_DOCKER_TMPFS_MB": "tmpfs_mb",
    }
    for env_name, field_name in docker_int_env.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            value = int(raw)
            if value <= 0:
                raise ValueError
        except ValueError:
            raise SystemExit(f"Config error: {env_name} must be a positive integer")
        setattr(config.execution.docker, field_name, value)

    docker_cpus = os.environ.get("KAMA_DOCKER_CPUS")
    if docker_cpus is not None:
        try:
            cpus = float(docker_cpus)
            if cpus <= 0:
                raise ValueError
        except ValueError:
            raise SystemExit("Config error: KAMA_DOCKER_CPUS must be a positive number")
        config.execution.docker.cpus = cpus

    run_env = {
        "KAMA_RUN_SLOW_SECONDS": "slow_run_seconds",
        "KAMA_RUN_STALLED_SECONDS": "stalled_seconds",
        "KAMA_RUN_TOOL_WARNING_SECONDS": "tool_warning_seconds",
        "KAMA_RUN_WARNING_SNOOZE_SECONDS": "warning_snooze_seconds",
        "KAMA_RUN_MONITOR_INTERVAL_SECONDS": "monitor_interval_seconds",
    }
    for env_name, field_name in run_env.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            run_value = float(raw)
            if run_value <= 0:
                raise ValueError
        except ValueError:
            raise SystemExit(f"Config error: {env_name} must be a positive number")
        setattr(config.runs, field_name, run_value)

    ignore_file = os.environ.get("KAMA_IGNORE_FILE")
    if ignore_file is None:
        ignore_file = os.environ.get("ignore_file")
    if ignore_file is not None:
        config.files.ignore_files = _merge_ignore_files(
            config.files.ignore_files,
            _parse_ignore_files(ignore_file),
        )


# 解析列表字面量或逗号分隔的忽略文件配置
def _parse_ignore_files(raw: str) -> list[str]:
    value = raw.strip()
    if not value:
        return []
    if value.startswith(("[", "(")):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise SystemExit(
                "Config error: KAMA_IGNORE_FILE/ignore_file must be a string list"
            ) from exc
        if not isinstance(parsed, (list, tuple)) or not all(
            isinstance(item, str) for item in parsed
        ):
            raise SystemExit("Config error: KAMA_IGNORE_FILE/ignore_file must be a string list")
        return _merge_ignore_files([], list(parsed))
    return _merge_ignore_files([], value.split(","))


# 按原有顺序合并并去重用户追加的忽略文件规则
def _merge_ignore_files(existing: list[str], additions: list[str]) -> list[str]:
    merged = list(existing)
    for item in additions:
        normalized = item.strip()
        if normalized and normalized not in merged:
            merged.append(normalized)
    return merged


# 将 [execution.docker] 严格解析到 Docker 执行配置
def _apply_docker_toml(config: DockerExecutionConfig, raw: object) -> None:
    if not isinstance(raw, dict):
        raise SystemExit("Config error: [execution.docker] must be a table")
    allowed = {
        "cli",
        "image",
        "network",
        "memory_mb",
        "cpus",
        "pids_limit",
        "tmpfs_mb",
        "user",
    }
    unknown = set(raw.keys()) - allowed
    if unknown:
        raise SystemExit(f"Unknown [execution.docker] keys: {', '.join(sorted(unknown))}")
    for key in ("cli", "image", "user"):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not value:
            raise SystemExit(f"Config error: execution.docker.{key} must be a non-empty string")
        setattr(config, key, value)
    if "network" in raw:
        network = raw["network"]
        if network not in ("none", "bridge"):
            raise SystemExit("Config error: execution.docker.network must be none or bridge")
        config.network = network
    for key in ("memory_mb", "pids_limit", "tmpfs_mb"):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, int) or value <= 0:
            raise SystemExit(
                f"Config error: execution.docker.{key} must be a positive integer"
            )
        setattr(config, key, value)
    if "cpus" in raw:
        cpus = raw["cpus"]
        if not isinstance(cpus, (int, float)) or cpus <= 0:
            raise SystemExit("Config error: execution.docker.cpus must be a positive number")
        config.cpus = float(cpus)
