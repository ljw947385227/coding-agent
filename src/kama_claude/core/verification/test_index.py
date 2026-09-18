from __future__ import annotations

import ast
import json
import os
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from kama_claude.core.git.checkpoint import CheckpointError, CheckpointManager
from kama_claude.core.verification.model import (
    SemanticChangeSummary,
    TestSelection,
    TestSelectionReason,
)
from kama_claude.core.verification.semantic.diff_analyzer import DiffAnalyzer

_SCHEMA_VERSION = 2
_ANALYZER_VERSION = 2
_DIFF_ANALYZER = DiffAnalyzer()
_MAX_SOURCE_BYTES = 1024 * 1024
_REBUILD_RATIO = 0.20
_IGNORED_PARTS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
_STRUCTURE_FILES = frozenset(
    {
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "setup.py",
        "tox.ini",
    }
)


@dataclass(frozen=True, slots=True)
class ImportBinding:
    module: str
    imported: str | None
    local: str


@dataclass(frozen=True, slots=True)
class PythonFileRecord:
    path: str
    module: str
    kind: Literal["source", "test"]
    imports: tuple[str, ...] = ()
    import_bindings: tuple[ImportBinding, ...] = ()
    tests: tuple[str, ...] = ()
    test_symbols: dict[str, tuple[str, ...]] = field(default_factory=dict)
    parse_error: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectTestIndex:
    schema_version: int
    analyzer: str
    analyzer_version: int
    tree_id: str
    project_root: str
    source_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    files: dict[str, PythonFileRecord]
    reverse_dependencies: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class TestIndexSummary:
    """持久化测试索引的轻量摘要，供 Session 创建和 TUI 展示。"""

    tree_id: str
    total_tests: int
    test_files: int


@dataclass(frozen=True, slots=True)
class ChangedFile:
    status: str
    path: str
    old_path: str | None = None


class TestIndexStore:
    # 初始化位于 Git 私有目录且不会污染工作树的测试索引存储
    def __init__(self, git_directory: Path) -> None:
        self._directory = git_directory / "kama-test-index"
        self._path = self._directory / "index.json"

    # 读取并校验最新测试索引，损坏或版本不兼容时返回 None 触发重建
    def load(self, project_root: str) -> ProjectTestIndex | None:
        if not self._path.is_file():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if (
                not isinstance(raw, dict)
                or raw.get("schema_version") != _SCHEMA_VERSION
                or raw.get("analyzer") != "python-ast"
                or raw.get("analyzer_version") != _ANALYZER_VERSION
                or raw.get("project_root") != project_root
            ):
                return None
            raw_files = raw.get("files")
            raw_reverse = raw.get("reverse_dependencies")
            if not isinstance(raw_files, dict) or not isinstance(raw_reverse, dict):
                return None
            files = {
                str(path): _record_from_json(str(path), value)
                for path, value in raw_files.items()
            }
            reverse = {
                str(module): tuple(str(item) for item in values)
                for module, values in raw_reverse.items()
                if isinstance(values, list)
            }
            return ProjectTestIndex(
                schema_version=_SCHEMA_VERSION,
                analyzer="python-ast",
                analyzer_version=_ANALYZER_VERSION,
                tree_id=str(raw["tree_id"]),
                project_root=project_root,
                source_roots=tuple(str(item) for item in raw.get("source_roots", [])),
                test_roots=tuple(str(item) for item in raw.get("test_roots", [])),
                files=files,
                reverse_dependencies=reverse,
            )
        except (KeyError, OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            return None

    # 以临时文件、刷盘和原子替换保存与确定 tree 绑定的最新索引
    def save(self, index: ProjectTestIndex) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = self._directory / f"index-{uuid.uuid4().hex}.tmp"
        payload = asdict(index)
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self._path)


class ProjectTestIndexManager:
    # 初始化绑定项目根目录和包含它的 Git 仓库的持久化索引管理器
    def __init__(self, project_root: Path, repository: Path) -> None:
        self._root = project_root.resolve()
        self._repository = repository.resolve()
        self._checkpoints = CheckpointManager(self._repository)
        relative = self._root.relative_to(self._repository)
        self._project_prefix = "" if relative == Path(".") else relative.as_posix()

    # 创建或刷新当前 tree 的测试结构表，并返回可供 UI 展示的测试总数
    async def initialize(self) -> TestIndexSummary:
        git_directory = await self._checkpoints.git_directory()
        store = TestIndexStore(git_directory)
        base_index = store.load(self._root.as_posix())
        for _attempt in range(2):
            before = await self._checkpoints.capture_state()
            index, _updated = await self._synchronize(base_index, before["worktree_tree"])
            after = await self._checkpoints.capture_state()
            if after["worktree_tree"] != before["worktree_tree"]:
                base_index = index
                continue
            store.save(index)
            await self._checkpoints.retain_index_tree(index.tree_id)
            return _index_summary(index)
        raise CheckpointError("workspace_changed_during_indexing")

    # 同步索引至稳定当前 tree，并从任务基线选择受影响的 Python 测试
    async def select(self, baseline_tree: str) -> TestSelection:
        try:
            return await self._select(baseline_tree)
        except (CheckpointError, OSError, PermissionError, ValueError) as exc:
            return TestSelection(
                strategy="full",
                baseline_tree=baseline_tree,
                indexed_tree="",
                fallback_reason=f"incremental_index_error:{type(exc).__name__}",
            )

    # 在工作区稳定性校验下完成一次索引同步和任务 diff 选择
    async def _select(self, baseline_tree: str) -> TestSelection:
        git_directory = await self._checkpoints.git_directory()
        store = TestIndexStore(git_directory)
        project_root_text = self._root.as_posix()
        base_index = store.load(project_root_text)
        for _attempt in range(2):
            before = await self._checkpoints.capture_state()
            index, updated = await self._synchronize(base_index, before["worktree_tree"])
            after = await self._checkpoints.capture_state()
            if after["worktree_tree"] != before["worktree_tree"]:
                continue
            store.save(index)
            await self._checkpoints.retain_index_tree(index.tree_id)
            task_changes = _parse_changes(
                await self._checkpoints.diff_trees(baseline_tree, index.tree_id)
            )
            project_changes = self._project_changes(task_changes)
            selection = _select_tests(index, baseline_tree, project_changes, updated)
            if selection.strategy != "related":
                return selection
            semantic_changes = await self._semantic_changes(
                index, baseline_tree, project_changes
            )
            return _apply_semantic_narrowing(
                index,
                selection,
                project_changes,
                semantic_changes,
            )
        return TestSelection(
            strategy="full",
            baseline_tree=baseline_tree,
            indexed_tree="",
            fallback_reason="workspace_changed_during_indexing",
        )

    # 根据旧索引 tree 到当前 tree 的 diff 选择增量更新或全量重建
    async def _synchronize(
        self,
        old: ProjectTestIndex | None,
        current_tree: str,
    ) -> tuple[ProjectTestIndex, tuple[str, ...]]:
        if old is None:
            index = await self._build_full(current_tree)
            return index, tuple(sorted(index.files))
        if old.tree_id == current_tree:
            return old, ()
        changes = _parse_changes(
            await self._checkpoints.diff_trees(old.tree_id, current_tree)
        )
        project_changes = self._project_changes(changes)
        updated = _changed_paths(project_changes)
        if _requires_full_rebuild(project_changes, len(old.files)):
            return await self._build_full(current_tree), updated
        files = dict(old.files)
        for change in project_changes:
            if change.old_path is not None:
                files.pop(change.old_path, None)
            if change.status.startswith("D"):
                files.pop(change.path, None)
                continue
            files.pop(change.path, None)
            record = self._read_record(change.path)
            if record is not None:
                files[change.path] = record
        return self._make_index(current_tree, files), updated

    # 从当前 tree 路径集合读取受支持 Python 文件并建立完整索引
    async def _build_full(self, tree: str) -> ProjectTestIndex:
        files: dict[str, PythonFileRecord] = {}
        for repository_path in await self._checkpoints.list_tree_paths(tree):
            project_path = self._to_project_path(repository_path)
            if project_path is None:
                continue
            record = self._read_record(project_path)
            if record is not None:
                files[project_path] = record
        return self._make_index(tree, files)

    # 从实时文件读取单个 Python 记录；前后 tree 校验保证最终保存时内容一致
    def _read_record(self, path: str) -> PythonFileRecord | None:
        relative = PurePosixPath(path)
        if relative.suffix != ".py" or any(part in _IGNORED_PARTS for part in relative.parts):
            return None
        candidate = self._root.joinpath(*relative.parts)
        if candidate.is_symlink() or not candidate.is_file():
            return None
        try:
            if candidate.stat().st_size > _MAX_SOURCE_BYTES:
                return None
            source = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        return _analyze_python(path, source, self._source_roots())

    # 根据当前项目目录约定生成索引元数据和反向依赖图
    def _make_index(
        self,
        tree: str,
        files: dict[str, PythonFileRecord],
    ) -> ProjectTestIndex:
        reverse: dict[str, set[str]] = {}
        for record in files.values():
            for imported in record.imports:
                reverse.setdefault(imported, set()).add(record.module)
        return ProjectTestIndex(
            schema_version=_SCHEMA_VERSION,
            analyzer="python-ast",
            analyzer_version=_ANALYZER_VERSION,
            tree_id=tree,
            project_root=self._root.as_posix(),
            source_roots=self._source_roots(),
            test_roots=self._test_roots(),
            files=files,
            reverse_dependencies={
                module: tuple(sorted(dependents))
                for module, dependents in sorted(reverse.items())
            },
        )

    # 返回 Python 模块解析使用的源码根目录约定
    def _source_roots(self) -> tuple[str, ...]:
        return ("src",) if (self._root / "src").is_dir() else (".",)

    # 识别 pytest 常见测试根；用于避免显式执行 tests 之外的 bench/typing 样例
    def _test_roots(self) -> tuple[str, ...]:
        return tuple(
            name for name in ("tests", "testing") if (self._root / name).is_dir()
        )

    # 将仓库相对路径过滤并转换为项目根目录相对路径
    def _to_project_path(self, repository_path: str) -> str | None:
        if not self._project_prefix:
            return repository_path
        prefix = self._project_prefix + "/"
        if not repository_path.startswith(prefix):
            return None
        return repository_path[len(prefix) :]

    # 将项目相对路径转换为 Git tree 使用的仓库相对路径
    def _to_repository_path(self, project_path: str) -> str:
        if not self._project_prefix:
            return project_path
        return f"{self._project_prefix}/{project_path}"

    # 将仓库级变化过滤并转换为当前验证项目内的路径
    def _project_changes(self, changes: tuple[ChangedFile, ...]) -> tuple[ChangedFile, ...]:
        converted: list[ChangedFile] = []
        for change in changes:
            path = self._to_project_path(change.path)
            old_path = (
                self._to_project_path(change.old_path)
                if change.old_path is not None
                else None
            )
            if path is None and old_path is None:
                continue
            converted.append(
                ChangedFile(
                    status=change.status,
                    path=path or old_path or change.path,
                    old_path=old_path,
                )
            )
        return tuple(converted)

    # 读取新旧 tree 并为源码变化生成函数、方法或结构级语义摘要
    async def _semantic_changes(
        self,
        index: ProjectTestIndex,
        baseline_tree: str,
        changes: tuple[ChangedFile, ...],
    ) -> tuple[SemanticChangeSummary, ...]:
        summaries: list[SemanticChangeSummary] = []
        for change in changes:
            if _is_test_path(change.path):
                continue
            record = index.files.get(change.path)
            if change.status != "M" or record is None:
                summaries.append(
                    SemanticChangeSummary(
                        path=change.path,
                        change_type=f"file_{change.status.lower()}",
                        risk="high",
                        safe_to_narrow=False,
                        detail="semantic narrowing only supports modified source files",
                    )
                )
                continue
            repository_path = self._to_repository_path(change.path)
            old_source = await self._checkpoints.read_tree_text(
                baseline_tree, repository_path, max_bytes=_MAX_SOURCE_BYTES
            )
            new_source = await self._checkpoints.read_tree_text(
                index.tree_id, repository_path, max_bytes=_MAX_SOURCE_BYTES
            )
            if old_source is None or new_source is None:
                summaries.append(
                    SemanticChangeSummary(
                        path=change.path,
                        change_type="oversized_source",
                        risk="high",
                        safe_to_narrow=False,
                        detail="source exceeds semantic parse byte limit",
                    )
                )
                continue
            patch = await self._checkpoints.diff_tree_patch(
                baseline_tree, index.tree_id, repository_path
            )
            summaries.append(
                _DIFF_ANALYZER.analyze(
                    change.path,
                    record.module,
                    patch,
                    old_source,
                    new_source,
                )
            )
        return tuple(summaries)


# 从持久化 JSON 字段恢复单文件 Python 索引记录
def _record_from_json(path: str, value: Any) -> PythonFileRecord:
    if not isinstance(value, dict):
        raise ValueError("invalid file record")
    raw_test_symbols = value.get("test_symbols", {})
    if not isinstance(raw_test_symbols, dict):
        raise ValueError("invalid test symbol record")
    return PythonFileRecord(
        path=path,
        module=str(value["module"]),
        kind="test" if value.get("kind") == "test" else "source",
        imports=tuple(str(item) for item in value.get("imports", [])),
        import_bindings=tuple(
            ImportBinding(
                module=str(item["module"]),
                imported=(str(item["imported"]) if item.get("imported") else None),
                local=str(item["local"]),
            )
            for item in value.get("import_bindings", [])
            if isinstance(item, dict)
        ),
        tests=tuple(str(item) for item in value.get("tests", [])),
        test_symbols={
            str(test_id): tuple(str(name) for name in names)
            for test_id, names in raw_test_symbols.items()
            if isinstance(names, list)
        },
        parse_error=(str(value["parse_error"]) if value.get("parse_error") else None),
    )


# 使用 Python AST 提取模块名、直接导入和静态测试 ID
def _analyze_python(
    path: str,
    source: str,
    source_roots: tuple[str, ...],
) -> PythonFileRecord:
    module = _module_name(path, source_roots)
    kind: Literal["source", "test"] = "test" if _is_test_path(path) else "source"
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return PythonFileRecord(
            path=path,
            module=module,
            kind=kind,
            parse_error=f"{exc.msg}:{exc.lineno or 0}",
        )
    imports: set[str] = set()
    bindings: list[ImportBinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
                bindings.append(
                    ImportBinding(
                        module=alias.name,
                        imported=None,
                        local=alias.asname or alias.name.split(".", 1)[0],
                    )
                )
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import(
                module,
                node.module,
                node.level,
                path.endswith("/__init__.py"),
            )
            if resolved:
                imports.add(resolved)
                for alias in node.names:
                    bindings.append(
                        ImportBinding(
                            module=resolved,
                            imported=alias.name,
                            local=alias.asname or alias.name,
                        )
                    )
    tests = _collect_tests(path, tree) if kind == "test" else ()
    test_symbols = _collect_test_symbols(path, tree) if kind == "test" else {}
    return PythonFileRecord(
        path=path,
        module=module,
        kind=kind,
        imports=tuple(sorted(imports)),
        import_bindings=tuple(
            sorted(bindings, key=lambda item: (item.module, item.imported or "", item.local))
        ),
        tests=tests,
        test_symbols=test_symbols,
    )


# 将项目相对 Python 路径转换为可与 import 语句匹配的模块名
def _module_name(path: str, source_roots: tuple[str, ...]) -> str:
    relative = PurePosixPath(path)
    parts = list(relative.with_suffix("").parts)
    if source_roots == ("src",) and parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[0] in {"tests", "testing"}:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


# 判断文件路径是否符合常见 Python 测试发现约定
def _is_test_path(path: str) -> bool:
    relative = PurePosixPath(path)
    name = relative.name
    # 与 pytest 默认 python_files 规则对齐。tests/ 中还会放置 conftest、
    # 类型检查样例和 helper；仅凭目录判定会把这些文件作为显式测试目标。
    return name.startswith("test_") or name.endswith("_test.py")


# 解析绝对或相对 import 并得到当前项目中的规范模块名
def _resolve_import(
    current_module: str,
    imported_module: str | None,
    level: int,
    is_package: bool,
) -> str:
    if level == 0:
        return imported_module or ""
    parts = current_module.split(".") if current_module else []
    if not is_package and parts:
        parts.pop()
    remove = max(0, level - 1)
    if remove:
        parts = parts[:-remove] if remove <= len(parts) else []
    if imported_module:
        parts.extend(imported_module.split("."))
    return ".".join(parts)


# 收集 pytest 常见函数和 Test 类方法的稳定静态测试 ID
def _collect_tests(path: str, tree: ast.Module) -> tuple[str, ...]:
    tests: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            tests.append(f"{path}::{node.name}")
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            before = len(tests)
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if child.name.startswith("test_"):
                    tests.append(f"{path}::{node.name}::{child.name}")
            # pytest 会收集从测试基类继承的方法；派生类可能只覆盖 fixture，
            # 因而源文件中没有自己的 test_*。文件级选择只需知道它是测试容器。
            if len(tests) == before and node.bases:
                tests.append(f"{path}::{node.name}")
    return tuple(tests)


# 建立测试 node ID 到局部名称引用的映射供语义候选缩小使用
def _collect_test_symbols(path: str, tree: ast.Module) -> dict[str, tuple[str, ...]]:
    module_functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    module_setup = set()
    autouse_refs = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_autouse_fixture(node):
                autouse_refs.update(_expanded_function_references(node, module_functions))
            continue
        if isinstance(node, ast.ClassDef):
            continue
        module_setup.update(_reference_names(node))

    result: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            references = _expanded_function_references(node, module_functions)
            references.update(module_setup)
            references.update(autouse_refs)
            result[f"{path}::{node.name}"] = tuple(sorted(references))
            continue
        if not isinstance(node, ast.ClassDef) or not node.name.startswith("Test"):
            continue
        class_functions = {
            child.name: child
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        helper_functions = {**module_functions, **class_functions}
        class_setup = set(_reference_names_without_functions(node))
        class_autouse = set()
        for function_node in class_functions.values():
            if _is_autouse_fixture(function_node):
                class_autouse.update(
                    _expanded_function_references(function_node, helper_functions)
                )
        found = False
        for class_body_node in node.body:
            if not isinstance(class_body_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not class_body_node.name.startswith("test_"):
                continue
            found = True
            references = _expanded_function_references(class_body_node, helper_functions)
            references.update(module_setup)
            references.update(autouse_refs)
            references.update(class_setup)
            references.update(class_autouse)
            result[f"{path}::{node.name}::{class_body_node.name}"] = tuple(
                sorted(references)
            )
        if not found and node.bases:
            references = module_setup | autouse_refs | class_setup | class_autouse
            result[f"{path}::{node.name}"] = tuple(sorted(references))
    return result


# 递归展开测试或 helper 调用的本地函数引用并防止循环调用
def _expanded_function_references(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    visited: set[str] | None = None,
) -> set[str]:
    seen = set(visited or ())
    if node.name in seen:
        return set()
    seen.add(node.name)
    references = _reference_names(node)
    for name in tuple(references):
        helper = functions.get(name)
        if helper is not None:
            references.update(_expanded_function_references(helper, functions, seen))
    return references


# 提取名称、属性、参数及标识符字符串作为快速搜索后的 AST 验证证据
def _reference_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.arg):
            names.add(child.arg)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value.isidentifier():
                names.add(child.value)
    return names


# 提取测试类的继承、装饰器和类级语句引用但跳过方法体
def _reference_names_without_functions(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for base in node.bases:
        names.update(_reference_names(base))
    for decorator in node.decorator_list:
        names.update(_reference_names(decorator))
    for child in node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.update(_reference_names(child))
    return names


# 判断 pytest fixture 装饰器是否显式声明 autouse=True
def _is_autouse_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        name = _call_name(decorator.func)
        if name not in {"fixture", "pytest.fixture"}:
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "autouse" and isinstance(keyword.value, ast.Constant):
                return keyword.value.value is True
    return False


# 将名称或属性调用目标还原为点分文本
def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


# 解析 git diff-tree 的 name-status 行并保留新增、修改、删除和重命名语义
def _parse_changes(lines: list[str]) -> tuple[ChangedFile, ...]:
    changes: list[ChangedFile] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status = fields[0]
        if status.startswith(("R", "C")) and len(fields) >= 3:
            changes.append(ChangedFile(status=status, path=fields[2], old_path=fields[1]))
        else:
            changes.append(ChangedFile(status=status, path=fields[1]))
    return tuple(changes)


# 汇总变化记录涉及的新旧项目路径供报告和索引更新使用
def _changed_paths(changes: tuple[ChangedFile, ...]) -> tuple[str, ...]:
    paths: set[str] = set()
    for change in changes:
        paths.add(change.path)
        if change.old_path is not None:
            paths.add(change.old_path)
    return tuple(sorted(paths))


# 判断配置变化或大比例变更是否需要放弃局部更新并重建索引
def _requires_full_rebuild(changes: tuple[ChangedFile, ...], file_count: int) -> bool:
    paths = _changed_paths(changes)
    if any(PurePosixPath(path).name in _STRUCTURE_FILES for path in paths):
        return True
    threshold = max(10, int(max(1, file_count) * _REBUILD_RATIO))
    return len(paths) > threshold


# 根据任务变更和最新反向依赖图选择测试，任何低置信度情况都降级全量
def _select_tests(
    index: ProjectTestIndex,
    baseline_tree: str,
    changes: tuple[ChangedFile, ...],
    updated: tuple[str, ...],
) -> TestSelection:
    changed_paths = _changed_paths(changes)
    if not changed_paths:
        return _full_selection(index, baseline_tree, changed_paths, updated, "no_task_changes")
    if any(PurePosixPath(path).name in _STRUCTURE_FILES for path in changed_paths):
        return _full_selection(
            index,
            baseline_tree,
            changed_paths,
            updated,
            "project_structure_changed",
        )
    if any(PurePosixPath(path).suffix != ".py" for path in changed_paths):
        return _full_selection(
            index,
            baseline_tree,
            changed_paths,
            updated,
            "unsupported_changed_file",
        )
    if any(PurePosixPath(path).name in {"conftest.py", "__init__.py"} for path in changed_paths):
        return _full_selection(
            index,
            baseline_tree,
            changed_paths,
            updated,
            "global_python_file_changed",
        )

    module_to_record = {record.module: record for record in index.files.values()}
    selected: dict[str, TestSelectionReason] = {}
    for path in changed_paths:
        record = index.files.get(path)
        if _is_test_path(path):
            if record is None or record.parse_error is not None:
                return _full_selection(
                    index, baseline_tree, changed_paths, updated, "changed_test_unparseable"
                )
            selected[path] = TestSelectionReason(path, "changed_test", path)
            continue
        if record is not None and record.parse_error is not None:
            return _full_selection(
                index, baseline_tree, changed_paths, updated, "changed_source_unparseable"
            )
        module = record.module if record is not None else _module_name(path, index.source_roots)
        queue: list[tuple[str, int]] = [(module, 0)]
        visited = {module}
        while queue:
            current, depth = queue.pop(0)
            for dependent in index.reverse_dependencies.get(current, ()):
                if dependent in visited:
                    continue
                visited.add(dependent)
                dependent_record = module_to_record.get(dependent)
                if dependent_record is None:
                    continue
                if dependent_record.kind == "test":
                    # tests/ 下还可能包含 conftest、类型样例和普通辅助模块；把它们
                    # 作为 pytest 路径会扩大甚至改变收集范围。只有静态发现到测试
                    # ID 的模块才是可执行选择目标，否则沿用最终的全量安全降级。
                    if dependent_record.tests and (
                        not index.test_roots
                        or _within_roots(dependent_record.path, index.test_roots)
                    ):
                        reason = "direct_import" if depth == 0 else "transitive_import"
                        selected.setdefault(
                            dependent_record.path,
                            TestSelectionReason(dependent_record.path, reason, path),
                        )
                    # 测试模块也可能被派生测试模块导入，不能在首个测试节点停止传播。
                    queue.append((dependent, depth + 1))
                else:
                    queue.append((dependent, depth + 1))
    if not selected:
        return _full_selection(
            index, baseline_tree, changed_paths, updated, "no_related_tests"
        )
    selected_tests = tuple(sorted(selected))
    return TestSelection(
        strategy="related",
        baseline_tree=baseline_tree,
        indexed_tree=index.tree_id,
        changed_files=changed_paths,
        selected_tests=selected_tests,
        reasons=tuple(selected[path] for path in selected_tests),
        index_updated_files=updated,
        total_tests=_test_count(index),
        selected_test_count=_selected_test_count(index, selected_tests),
    )


# 对低风险函数体修改按测试中的受影响 import 引用缩小到 pytest node ID
def _apply_semantic_narrowing(
    index: ProjectTestIndex,
    selection: TestSelection,
    changes: tuple[ChangedFile, ...],
    semantic_changes: tuple[SemanticChangeSummary, ...],
) -> TestSelection:
    selection = replace(selection, semantic_changes=semantic_changes)
    if not semantic_changes or any(not change.safe_to_narrow for change in semantic_changes):
        return selection
    source_paths = tuple(
        change.path for change in changes if not _is_test_path(change.path)
    )
    impacted_modules = _impacted_modules(index, source_paths)
    direct_modules = {
        record.module
        for path in source_paths
        if (record := index.files.get(path)) is not None
    }
    target_names = {
        symbol.rsplit(".", 1)[-1]
        for change in semantic_changes
        for symbol in change.new_symbols
    }
    if not impacted_modules:
        return selection
    semantic_source = ",".join(
        symbol
        for change in semantic_changes
        for symbol in change.new_symbols
    )
    narrowed: dict[str, TestSelectionReason] = {}
    original_reasons = {reason.test: reason for reason in selection.reasons}
    for target in selection.selected_tests:
        path = target.split("::", 1)[0]
        if path in source_paths or _is_changed_test_path(path, changes):
            narrowed[target] = original_reasons.get(
                target,
                TestSelectionReason(target, "changed_test", path),
            )
            continue
        record = index.files.get(path)
        if record is None or record.kind != "test" or not record.test_symbols:
            narrowed[target] = original_reasons.get(
                target,
                TestSelectionReason(target, "semantic_file_fallback", path),
            )
            continue
        bindings = tuple(
            binding
            for binding in record.import_bindings
            if _binding_matches_direct_change(
                binding,
                direct_modules=direct_modules,
                target_names=target_names,
            )
        )
        local_names = {binding.local for binding in bindings if binding.local != "*"}
        if not bindings or "*" in {binding.local for binding in bindings}:
            narrowed[target] = TestSelectionReason(
                target, "semantic_file_fallback", semantic_source or path
            )
            continue
        matched = tuple(
            test_id
            for test_id, references in sorted(record.test_symbols.items())
            if local_names.intersection(references)
        )
        if not matched:
            narrowed[target] = TestSelectionReason(
                target, "semantic_file_fallback", semantic_source or path
            )
            continue
        for test_id in matched:
            narrowed[test_id] = TestSelectionReason(
                test_id,
                "semantic_import_reference",
                semantic_source or path,
            )
    selected = tuple(sorted(narrowed))
    if not selected:
        return selection
    return replace(
        selection,
        selected_tests=selected,
        reasons=tuple(narrowed[target] for target in selected),
        selected_test_count=_selected_test_count(index, selected),
    )


# 沿现有模块反向依赖图收集受源码变化影响的生产与测试模块
def _impacted_modules(
    index: ProjectTestIndex, source_paths: tuple[str, ...]
) -> set[str]:
    modules = {
        record.module
        for path in source_paths
        if (record := index.files.get(path)) is not None
    }
    queue = list(modules)
    while queue:
        current = queue.pop(0)
        for dependent in index.reverse_dependencies.get(current, ()):
            if dependent not in modules:
                modules.add(dependent)
                queue.append(dependent)
    return modules


# 判断测试 import 绑定是否指向任一受影响模块或其父子模块
def _binding_reaches_impacted_module(
    binding: ImportBinding, impacted_modules: set[str]
) -> bool:
    return any(
        binding.module == module
        or binding.module.startswith(module + ".")
        or module.startswith(binding.module + ".")
        for module in impacted_modules
    )


# 仅把直接导入被修改函数或方法的测试缩小到 node，避免同模块无关函数被误选
def _binding_matches_direct_change(
    binding: ImportBinding,
    *,
    direct_modules: set[str],
    target_names: set[str],
) -> bool:
    if binding.module not in direct_modules:
        return False
    if binding.imported is None:
        return True
    return binding.imported in target_names or binding.imported == "*"


# 判断路径是否是当前任务直接修改的测试文件
def _is_changed_test_path(path: str, changes: tuple[ChangedFile, ...]) -> bool:
    return any(
        _is_test_path(candidate)
        and path == candidate
        for change in changes
        for candidate in (change.path, change.old_path)
        if candidate is not None
    )


# 判断路径是否位于项目识别出的常规测试根目录
def _within_roots(path: str, roots: tuple[str, ...]) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and parts[0] in roots


# 构造携带明确降级原因且保持全量验证命令的测试选择结果
def _full_selection(
    index: ProjectTestIndex,
    baseline_tree: str,
    changed_paths: tuple[str, ...],
    updated: tuple[str, ...],
    reason: str,
) -> TestSelection:
    return TestSelection(
        strategy="full",
        baseline_tree=baseline_tree,
        indexed_tree=index.tree_id,
        changed_files=changed_paths,
        index_updated_files=updated,
        fallback_reason=reason,
        total_tests=_test_count(index),
        selected_test_count=_test_count(index),
    )


# 统计索引中可被 pytest 收集的稳定测试节点数量
def _test_count(index: ProjectTestIndex) -> int:
    return sum(len(record.tests) for record in index.files.values() if record.kind == "test")


# 将文件级 pytest 目标和语义 node ID 统一换算为索引测试节点数
def _selected_test_count(index: ProjectTestIndex, selected: tuple[str, ...]) -> int:
    count = 0
    for target in selected:
        path = target.split("::", 1)[0]
        if "::" in target:
            count += 1
            continue
        record = index.files.get(path)
        count += len(record.tests) if record is not None and record.tests else 1
    return count


# 将索引转换为创建 Session 时需要的轻量测试规模摘要
def _index_summary(index: ProjectTestIndex) -> TestIndexSummary:
    test_records = [record for record in index.files.values() if record.kind == "test"]
    return TestIndexSummary(
        tree_id=index.tree_id,
        total_tests=sum(len(record.tests) for record in test_records),
        test_files=len(test_records),
    )
