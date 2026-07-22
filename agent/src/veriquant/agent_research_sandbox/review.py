"""Mechanical static review for agent-authored research Python.

Passing this gate means only that source is eligible for a future OS-isolated
runner.  It never executes, imports, compiles, or grants authority to code.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass

from .contracts import (
    ExperimentReviewDecision,
    ExperimentReviewRequest,
    MechanicalReviewStatus,
    ReviewFailureCode,
    ReviewFinding,
    ReviewerSuggestion,
    ReviewSeverity,
)


_FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "aiohttp", "asyncio", "builtins", "ctypes", "http", "httpx", "importlib",
        "inspect", "io", "marshal", "multiprocessing", "os", "pathlib", "pickle",
        "requests", "shelve", "shutil", "socket", "subprocess", "sys", "tempfile",
        "threading", "urllib", "webbrowser",
    }
)
_DYNAMIC_CALLS = frozenset(
    {"eval", "exec", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr"}
)
_IO_CALLS = frozenset(
    {
        "open", "input", "read_csv", "read_excel", "read_feather", "read_fwf",
        "read_hdf", "read_html", "read_json", "read_orc", "read_parquet", "read_pickle",
        "read_sas", "read_sql", "read_stata", "read_table", "read_text", "write_text",
        "read_bytes", "write_bytes", "to_csv", "to_excel", "to_feather", "to_hdf",
        "to_json", "to_orc", "to_parquet", "to_pickle", "to_sql", "load", "save",
        "savez", "savez_compressed", "fromfile", "tofile", "memmap",
    }
)
_NETWORK_CALLS = frozenset(
    {"connect", "create_connection", "getaddrinfo", "urlopen", "urlretrieve", "request", "get", "post", "put", "patch", "delete", "head"}
)
_PROCESS_CALLS = frozenset(
    {"system", "popen", "run", "call", "check_call", "check_output", "Popen", "spawn", "fork", "startfile"}
)
_ENV_CALLS = frozenset({"getenv", "get_exec_path", "putenv", "unsetenv"})
_SERIALIZATION_CALLS = frozenset({"loads", "load", "dumps", "dump"})
_LABEL_TOKENS = frozenset(
    {"label", "labels", "future_return", "forward_return", "target_return", "sealed", "test_metric", "test_metrics"}
)
_PATH_PATTERN = re.compile(
    r"(?i)(?:^[a-z]:[\\/]|^\\\\|(?:^|[\\/])\.\.(?:[\\/]|$)|file://|~[\\/]|\\\\\.\\|:[^\\/:]+$)"
)


def _label_bearing_name(value: str) -> bool:
    lowered = value.strip().casefold()
    return lowered in _LABEL_TOKENS or any(
        token in lowered for token in ("future_return", "forward_return", "target_return", "sealed_metric")
    )


def _source_hash(source: str) -> str:
    return "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _root_name(node: ast.AST) -> str | None:
    cursor = node
    while isinstance(cursor, ast.Attribute):
        cursor = cursor.value
    return cursor.id if isinstance(cursor, ast.Name) else None


def _negative_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
        and not isinstance(node.operand.value, bool)
    ) or (isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool) and node.value < 0)


@dataclass
class _Inspector(ast.NodeVisitor):
    request: ExperimentReviewRequest

    def __post_init__(self) -> None:
        self.findings: list[ReviewFinding] = []
        self.import_aliases: dict[str, str] = {}

    def add(self, code: ReviewFailureCode, message: str, node: ast.AST | None = None) -> None:
        finding = ReviewFinding(
            code=code,
            severity=ReviewSeverity.ERROR,
            message=message,
            line=getattr(node, "lineno", None),
            column=getattr(node, "col_offset", None),
        )
        key = (finding.code, finding.line, finding.column, finding.message)
        if key not in {(item.code, item.line, item.column, item.message) for item in self.findings}:
            self.findings.append(finding)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root in _FORBIDDEN_IMPORT_ROOTS or root not in self.request.policy.allowed_import_roots:
                self.add(ReviewFailureCode.FORBIDDEN_IMPORT, f"import root {root!r} is not allowed", node)
            self.import_aliases[alias.asname or root] = root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".", 1)[0]
        if node.level or not root or root in _FORBIDDEN_IMPORT_ROOTS or root not in self.request.policy.allowed_import_roots:
            self.add(ReviewFailureCode.FORBIDDEN_IMPORT, f"import root {root or '<relative>'!r} is not allowed", node)
        for alias in node.names:
            self.import_aliases[alias.asname or alias.name] = root
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        lowered = node.id.casefold()
        if "__" in node.id:
            self.add(ReviewFailureCode.DUNDER_ACCESS_FORBIDDEN, "dunder names are forbidden", node)
        if lowered in _LABEL_TOKENS or any(token in lowered for token in ("future_return", "forward_return", "target_return", "sealed_")):
            self.add(ReviewFailureCode.LABEL_ACCESS_FORBIDDEN, "labels, future returns, and sealed material are unavailable to score generation", node)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if "__" in node.attr:
            self.add(ReviewFailureCode.DUNDER_ACCESS_FORBIDDEN, "dunder attributes are forbidden", node)
        lowered = node.attr.casefold()
        if lowered in _LABEL_TOKENS or any(token in lowered for token in ("future_return", "forward_return", "target_return", "sealed_")):
            self.add(ReviewFailureCode.LABEL_ACCESS_FORBIDDEN, "labels, future returns, and sealed material are unavailable to score generation", node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            text = node.value.strip()
            if _PATH_PATTERN.search(text):
                self.add(ReviewFailureCode.PATH_ACCESS_FORBIDDEN, "absolute, traversal, device, URI, or alternate-stream path is forbidden", node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # A label-shaped string is forbidden when it is used to address data.
        # It is harmless as a result-table metric label such as
        # ``metric_name='mean_future_return'``; Evidence authority is still
        # outside the generated program.
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and _label_bearing_name(key.value):
            self.add(ReviewFailureCode.LABEL_ACCESS_FORBIDDEN, "label-bearing data keys are forbidden", key)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        lowered = name.casefold()
        root = _root_name(node.func)
        imported_root = self.import_aliases.get(root or "", root or "")
        if name in _DYNAMIC_CALLS or lowered in {item.casefold() for item in _DYNAMIC_CALLS}:
            code = ReviewFailureCode.DYNAMIC_IMPORT_FORBIDDEN if name == "__import__" else ReviewFailureCode.DYNAMIC_EXECUTION_FORBIDDEN
            self.add(code, f"dynamic call {name!r} is forbidden", node)
        if lowered in {item.casefold() for item in _IO_CALLS}:
            self.add(ReviewFailureCode.FILE_IO_FORBIDDEN, f"file/data-loader call {name!r} is forbidden; use injected read-only tables", node)
        # Method names such as ``dict.get`` are ordinary in numerical code.
        # Treat a network verb as privileged only when it is a bare call or
        # its receiver resolves to an imported network module.  Forbidden
        # network imports are independently rejected in ``visit_Import``.
        network_roots = {"requests", "httpx", "aiohttp", "urllib", "socket", "http"}
        bare_call = isinstance(node.func, ast.Name)
        if imported_root in network_roots or (bare_call and lowered in {item.casefold() for item in _NETWORK_CALLS}):
            self.add(ReviewFailureCode.NETWORK_FORBIDDEN, f"network call {name!r} is forbidden", node)
        if lowered in {item.casefold() for item in _PROCESS_CALLS} or imported_root in {"subprocess", "multiprocessing", "os"}:
            self.add(ReviewFailureCode.PROCESS_FORBIDDEN, f"process call {name!r} is forbidden", node)
        if lowered in {item.casefold() for item in _ENV_CALLS} or (imported_root == "os" and lowered == "environ"):
            self.add(ReviewFailureCode.ENVIRONMENT_ACCESS_FORBIDDEN, "environment access is forbidden", node)
        if imported_root in {"pickle", "marshal", "shelve"} or (lowered in {item.casefold() for item in _SERIALIZATION_CALLS} and imported_root in {"pickle", "marshal", "shelve"}):
            self.add(ReviewFailureCode.UNSAFE_SERIALIZATION_FORBIDDEN, "unsafe object serialization is forbidden", node)
        if lowered in {"shift", "diff", "pct_change"}:
            periods = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg in {"periods", "n"}), None)
            if periods is not None and _negative_literal(periods):
                self.add(ReviewFailureCode.FUTURE_REFERENCE_FORBIDDEN, f"negative {name} lookahead is forbidden", node)
        if lowered == "get" and node.args:
            key = node.args[0]
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and _label_bearing_name(key.value):
                self.add(ReviewFailureCode.LABEL_ACCESS_FORBIDDEN, "label-bearing data keys are forbidden", key)
        self.generic_visit(node)


class ExperimentReviewGate:
    """Parse and mechanically reject unsafe experiment source without executing it."""

    def review(
        self,
        request: ExperimentReviewRequest,
        *,
        reviewer_suggestions: tuple[ReviewerSuggestion, ...] = (),
    ) -> ExperimentReviewDecision:
        if not isinstance(request, ExperimentReviewRequest):
            raise TypeError("request must be ExperimentReviewRequest")
        if not isinstance(reviewer_suggestions, tuple) or not all(isinstance(item, ReviewerSuggestion) for item in reviewer_suggestions):
            raise TypeError("reviewer_suggestions must be a ReviewerSuggestion tuple")

        findings: list[ReviewFinding] = []
        actual_source_hash = _source_hash(request.source_text)
        if actual_source_hash != request.code_identity.source_hash:
            findings.append(ReviewFinding(code=ReviewFailureCode.SOURCE_HASH_MISMATCH, severity=ReviewSeverity.ERROR, message="source text does not match ExperimentCodeIdentity"))
        encoded = request.source_text.encode("utf-8")
        if len(encoded) > request.policy.max_source_bytes:
            findings.append(ReviewFinding(code=ReviewFailureCode.AST_COMPLEXITY_EXCEEDED, severity=ReviewSeverity.ERROR, message="source exceeds policy byte budget"))

        try:
            tree = ast.parse(request.source_text, mode="exec")
        except SyntaxError as exc:
            findings.append(ReviewFinding(code=ReviewFailureCode.SYNTAX_ERROR, severity=ReviewSeverity.ERROR, message="source is not valid Python syntax", line=exc.lineno, column=exc.offset))
        else:
            node_count = sum(1 for _ in ast.walk(tree))
            if node_count > request.policy.max_ast_nodes:
                findings.append(ReviewFinding(code=ReviewFailureCode.AST_COMPLEXITY_EXCEEDED, severity=ReviewSeverity.ERROR, message="AST exceeds policy node budget"))
            inspector = _Inspector(request)
            inspector.visit(tree)
            findings.extend(inspector.findings)
            entrypoints = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == request.code_identity.entrypoint
            ]
            invalid_entrypoint = len(entrypoints) != 1 or isinstance(entrypoints[0], ast.AsyncFunctionDef)
            if not invalid_entrypoint:
                arguments = entrypoints[0].args
                invalid_entrypoint = bool(
                    arguments.vararg
                    or arguments.kwarg
                    or arguments.kwonlyargs
                    or len(arguments.posonlyargs) + len(arguments.args) not in {2, 3}
                )
            if invalid_entrypoint:
                findings.append(
                    ReviewFinding(
                        code=ReviewFailureCode.ENTRYPOINT_INVALID,
                        severity=ReviewSeverity.ERROR,
                        message="source must define exactly one synchronous run_research entrypoint with two or three fixed arguments",
                    )
                )

        stable = tuple(sorted(findings, key=lambda item: (item.line or 0, item.column or 0, item.code.value, item.message)))
        status = MechanicalReviewStatus.REJECTED if stable else MechanicalReviewStatus.ACCEPTED
        return ExperimentReviewDecision(
            experiment_id=request.experiment_id,
            source_hash=actual_source_hash,
            policy_hash=request.policy.artifact_hash,
            amendment_hash=request.amendment.artifact_hash,
            status=status,
            findings=stable,
            reviewer_suggestions=reviewer_suggestions,
            revision_number=request.revision_number,
            may_submit_once_more=status is MechanicalReviewStatus.REJECTED and request.revision_number == 0,
        )


__all__ = ["ExperimentReviewGate"]
