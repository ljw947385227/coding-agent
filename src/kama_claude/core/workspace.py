from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkspaceBoundary:
    root: Path

    # 从存在的目录建立规范化绝对工作区边界
    @classmethod
    def from_path(cls, root: str | Path) -> WorkspaceBoundary:
        path = Path(root).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"workspace does not exist: {path}")
        if not path.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {path}")
        return cls(path)

    # 将相对或绝对输入解析到工作区内并拒绝符号链接和父路径逃逸
    def resolve(
        self,
        path_text: str,
        *,
        must_exist: bool = True,
    ) -> Path:
        raw = Path(path_text).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise PermissionError(
                f"path escapes workspace {self.root.as_posix()}: {path_text}"
            )
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"no such path in workspace: {path_text}")
        return resolved

    # 返回适合持久化和跨平台展示的绝对工作区字符串
    def as_posix(self) -> str:
        return self.root.as_posix()
