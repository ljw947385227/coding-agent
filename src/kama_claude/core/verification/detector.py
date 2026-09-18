from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from kama_claude.core.verification.model import (
    VerificationCheck,
    VerificationKind,
    VerificationPlan,
)

_KIND_ORDER: tuple[VerificationKind, ...] = ("test", "lint", "typecheck", "build")


# 检测项目清单并生成按 test、lint、typecheck、build 排序的验证计划
def discover_verification_plan(
    root: Path,
    selected: Sequence[VerificationKind] | None = None,
) -> VerificationPlan:
    root = root.resolve()
    wanted = set(selected or _KIND_ORDER)
    checks: list[VerificationCheck] = []
    ecosystems: list[str] = []
    warnings: list[str] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        ecosystems.append("python")
        python_checks, python_warnings = _detect_python(root, pyproject)
        checks.extend(python_checks)
        warnings.extend(python_warnings)

    package_json = root / "package.json"
    if package_json.is_file():
        ecosystems.append("node")
        node_checks, node_warnings = _detect_node(root, package_json)
        checks.extend(node_checks)
        warnings.extend(node_warnings)

    if (root / "Cargo.toml").is_file():
        ecosystems.append("rust")
        checks.extend(_detect_rust())

    if (root / "go.mod").is_file():
        ecosystems.append("go")
        checks.extend(_detect_go())

    if not ecosystems:
        warnings.append("No supported project manifest found in the selected directory.")

    filtered = [check for check in checks if check.kind in wanted]
    filtered.sort(key=lambda check: (_KIND_ORDER.index(check.kind), check.ecosystem))
    available = {check.kind for check in filtered}
    for kind in _KIND_ORDER:
        if kind in wanted and kind not in available:
            warnings.append(f"No {kind} check was detected.")
    return VerificationPlan(
        root=root.as_posix(),
        ecosystems=tuple(ecosystems),
        checks=tuple(filtered),
        warnings=tuple(dict.fromkeys(warnings)),
    )


# 从 pyproject 配置和依赖中推导 Python 验证命令
def _detect_python(
    root: Path, pyproject: Path
) -> tuple[list[VerificationCheck], list[str]]:
    warnings: list[str] = []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        data = {}
        warnings.append(f"Failed to parse pyproject.toml: {exc}")

    tool = data.get("tool", {}) if isinstance(data, dict) else {}
    tool_names = set(tool) if isinstance(tool, dict) else set()
    dependencies = {item.lower() for item in _iter_dependency_strings(data)}
    prefix = _python_prefix(root)
    source_targets = [name for name in ("src", "tests") if (root / name).exists()]
    lint_targets = source_targets or ["."]
    type_targets = ["src"] if (root / "src").exists() else ["."]
    test_targets = ["tests"] if (root / "tests").exists() else []
    checks: list[VerificationCheck] = []

    if test_targets or "pytest" in tool_names or _has_dependency(dependencies, "pytest"):
        checks.append(
            VerificationCheck(
                "test",
                "python",
                (*prefix, "pytest", *test_targets, "-q"),
                "pyproject.toml",
                "pytest",
            )
        )
    if "ruff" in tool_names or _has_dependency(dependencies, "ruff"):
        checks.append(
            VerificationCheck(
                "lint",
                "python",
                (*prefix, "ruff", "check", *lint_targets),
                "pyproject.toml",
                "ruff",
            )
        )
    if "mypy" in tool_names or _has_dependency(dependencies, "mypy"):
        checks.append(
            VerificationCheck(
                "typecheck",
                "python",
                (*prefix, "mypy", *type_targets),
                "pyproject.toml",
                "mypy",
            )
        )
    if isinstance(data, dict) and isinstance(data.get("build-system"), dict):
        build_command: tuple[str, ...]
        if prefix[:2] == ("uv", "run"):
            build_command = ("uv", "build")
        elif prefix[:2] == ("poetry", "run"):
            build_command = ("poetry", "build")
        else:
            build_command = ("python", "-m", "build")
        checks.append(
            VerificationCheck(
                "build",
                "python",
                build_command,
                "pyproject.toml",
                "python-build",
            )
        )
    return checks, warnings


# 递归提取 pyproject 中项目、可选和开发依赖字符串
def _iter_dependency_strings(data: object) -> Iterable[str]:
    if not isinstance(data, dict):
        return
    project = data.get("project")
    if isinstance(project, dict):
        yield from _strings_in(project.get("dependencies"))
        yield from _strings_in(project.get("optional-dependencies"))
    yield from _strings_in(data.get("dependency-groups"))


# 从嵌套列表或映射中递归产出依赖字符串
def _strings_in(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)


# 判断规范化依赖字符串是否声明给定包名
def _has_dependency(dependencies: set[str], package: str) -> bool:
    return any(_package_name(value) == package for value in dependencies)


# 从 PEP 508 风格依赖字符串中提取包名部分
def _package_name(value: str) -> str:
    end = len(value)
    for marker in "<>=!~[ ;":
        position = value.find(marker)
        if position >= 0:
            end = min(end, position)
    return value[:end].strip().replace("_", "-")


# 根据锁文件选择 Python 命令运行前缀
def _python_prefix(root: Path) -> tuple[str, ...]:
    if (root / "uv.lock").is_file():
        return ("uv", "run")
    if (root / "poetry.lock").is_file():
        return ("poetry", "run")
    for relative in (Path(".venv/Scripts/python.exe"), Path(".venv/bin/python")):
        interpreter = root / relative
        if interpreter.is_file():
            return (str(interpreter.resolve()), "-m")
    return (sys.executable, "-m")


# 从 package.json scripts 和锁文件推导 Node 验证命令
def _detect_node(
    root: Path, package_json: Path
) -> tuple[list[VerificationCheck], list[str]]:
    try:
        raw = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], [f"Failed to parse package.json: {exc}"]
    scripts = raw.get("scripts", {}) if isinstance(raw, dict) else {}
    if not isinstance(scripts, dict):
        return [], ["package.json scripts must be an object."]
    manager = _node_package_manager(root, raw)
    checks: list[VerificationCheck] = []
    script_names: dict[VerificationKind, tuple[str, ...]] = {
        "test": ("test",),
        "lint": ("lint",),
        "typecheck": ("typecheck", "type-check", "check-types"),
        "build": ("build",),
    }
    for kind, candidates in script_names.items():
        script = next((name for name in candidates if isinstance(scripts.get(name), str)), None)
        if script is not None:
            checks.append(
                VerificationCheck(
                    kind,
                    "node",
                    (manager, "run", script),
                    f"package.json#scripts.{script}",
                    _node_script_tool(str(scripts[script]), kind),
                )
            )
    return checks, []


# 根据 packageManager 字段或锁文件选择 Node 包管理器
def _node_package_manager(root: Path, package_data: dict[str, Any]) -> str:
    declared = package_data.get("packageManager")
    if isinstance(declared, str) and declared:
        name = declared.split("@", 1)[0]
        if name in {"npm", "pnpm", "yarn", "bun"}:
            return name
    for lock_file, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("bun.lock", "bun"),
    ):
        if (root / lock_file).is_file():
            return manager
    return "npm"


# 从 package.json 脚本文本和检查阶段推断具体 Node 工具
def _node_script_tool(script: str, kind: VerificationKind) -> str:
    lowered = script.lower()
    for tool in ("vitest", "jest", "eslint", "tsc"):
        if tool in lowered:
            return tool
    return f"node-{kind}"


# 返回 Rust 项目的标准验证命令集合
def _detect_rust() -> list[VerificationCheck]:
    return [
        VerificationCheck("test", "rust", ("cargo", "test"), "Cargo.toml", "cargo"),
        VerificationCheck(
            "lint",
            "rust",
            ("cargo", "clippy", "--all-targets", "--all-features", "--", "-D", "warnings"),
            "Cargo.toml",
            "cargo",
        ),
        VerificationCheck("typecheck", "rust", ("cargo", "check"), "Cargo.toml", "cargo"),
        VerificationCheck("build", "rust", ("cargo", "build"), "Cargo.toml", "cargo"),
    ]


# 返回 Go 项目的标准验证命令集合
def _detect_go() -> list[VerificationCheck]:
    return [
        VerificationCheck("test", "go", ("go", "test", "./..."), "go.mod", "go"),
        VerificationCheck("lint", "go", ("go", "vet", "./..."), "go.mod", "go"),
        VerificationCheck("build", "go", ("go", "build", "./..."), "go.mod", "go"),
    ]
