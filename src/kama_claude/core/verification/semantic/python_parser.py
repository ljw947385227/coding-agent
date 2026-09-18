from __future__ import annotations

import ast

from kama_claude.core.verification.semantic.model import UnifiedSymbol


class PythonParser:
    # 返回 Python 解析器的稳定语言标识
    @property
    def language(self) -> str:
        return "python"

    # 返回 Python 语法文件后缀供解析器注册表路由
    @property
    def extensions(self) -> tuple[str, ...]:
        return (".py", ".pyi")

    # 使用标准库 AST 解析函数、方法和类为统一符号模型
    def parse_symbols(
        self, source: str, *, path: str, module: str
    ) -> tuple[UnifiedSymbol, ...]:
        tree = ast.parse(source, filename=path)
        visitor = _SymbolVisitor(module)
        visitor.visit(tree)
        return tuple(visitor.symbols)


class _SymbolVisitor(ast.NodeVisitor):
    # 初始化 Python 符号访问器并维护类、函数和嵌套作用域栈
    def __init__(self, module: str) -> None:
        self._module = module
        self._scope: list[tuple[str, str]] = []
        self.symbols: list[UnifiedSymbol] = []

    # 记录类范围并继续解析类体中的方法和嵌套定义
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.symbols.append(
            _symbol(
                module=self._module,
                scope=self._scope,
                node=node,
                kind="class",
                signature=_class_signature(node),
            )
        )
        self._scope.append((node.name, "class"))
        self.generic_visit(node)
        self._scope.pop()

    # 记录同步函数或方法并继续解析嵌套定义
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    # 记录异步函数或方法并继续解析嵌套定义
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    # 根据父作用域区分函数与方法并保存稳定签名
    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = "method" if self._scope and self._scope[-1][1] == "class" else "function"
        self.symbols.append(
            _symbol(
                module=self._module,
                scope=self._scope,
                node=node,
                kind=kind,
                signature=_function_signature(node),
            )
        )
        self._scope.append((node.name, kind))
        self.generic_visit(node)
        self._scope.pop()


# 将 AST 节点转换成统一符号并将装饰器纳入范围
def _symbol(
    *,
    module: str,
    scope: list[tuple[str, str]],
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    kind: str,
    signature: str,
) -> UnifiedSymbol:
    parts = [module, *(item[0] for item in scope), node.name]
    qualified_name = ".".join(part for part in parts if part)
    decorator_lines = [item.lineno for item in node.decorator_list]
    start_line = min((node.lineno, *decorator_lines))
    body_start_line = min(
        (item.lineno for item in node.body),
        default=node.end_lineno or node.lineno,
    )
    return UnifiedSymbol(
        language="python",
        kind=kind,
        name=node.name,
        qualified_name=qualified_name,
        start_line=start_line,
        end_line=node.end_lineno or node.lineno,
        body_start_line=body_start_line,
        signature=signature,
    )


# 生成忽略函数体但保留签名、装饰器和类型参数的稳定摘要
def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    payload = (
        ast.dump(node.args, include_attributes=False),
        ast.dump(node.returns, include_attributes=False) if node.returns else "",
        tuple(ast.dump(item, include_attributes=False) for item in node.decorator_list),
        tuple(
            ast.dump(item, include_attributes=False)
            for item in getattr(node, "type_params", ())
        ),
        isinstance(node, ast.AsyncFunctionDef),
    )
    return repr(payload)


# 生成包含基类、关键字、装饰器和类型参数的类摘要
def _class_signature(node: ast.ClassDef) -> str:
    payload = (
        tuple(ast.dump(item, include_attributes=False) for item in node.bases),
        tuple(ast.dump(item, include_attributes=False) for item in node.keywords),
        tuple(ast.dump(item, include_attributes=False) for item in node.decorator_list),
        tuple(
            ast.dump(item, include_attributes=False)
            for item in getattr(node, "type_params", ())
        ),
    )
    return repr(payload)
