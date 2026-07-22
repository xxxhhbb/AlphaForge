"""Final, recoverable receipt for the converged AlphaForge research product.

This harness performs no research and computes no financial metric.  It binds
already completed representative, specialist, dynamic-data, test and security
receipts into one truthful product verdict.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from ..research_data_gateway.models import canonical_hash


_SECRET = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/=-]{8,}|sk-[a-z0-9_-]{8,}|"
    r"authorization\s*:\s*[^\s]+|api[_-]?key\s*[:=]\s*[^\s]+)"
)
_FORBIDDEN_ROUTES = ("/live", "broker", "order", "oauth", "mandate")


class ProductFinalizationHarness:
    def __init__(
        self,
        *,
        output_root: str | Path,
        qualification_receipt: str | Path,
        dynamic_data_receipt: str | Path,
        representative_receipt: str | Path,
        static_coverage_receipt: str | Path,
        source_roots: tuple[str | Path, ...],
        tests_passed: int,
        test_command: str,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.qualification_receipt = Path(qualification_receipt).resolve()
        self.dynamic_data_receipt = Path(dynamic_data_receipt).resolve()
        self.representative_receipt = Path(representative_receipt).resolve()
        self.static_coverage_receipt = Path(static_coverage_receipt).resolve()
        self.source_roots = tuple(Path(item).resolve() for item in source_roots)
        if not self.source_roots:
            raise ValueError("at least one trusted source root is required")
        self.tests_passed = tests_passed
        self.test_command = test_command
        self.output_root.mkdir(parents=True, exist_ok=True)

    def run(self) -> Mapping[str, Any]:
        cached = self._load_final()
        if cached is not None:
            return {**cached, "replayed_without_recomputation": True}
        self._write_progress("IN_PROGRESS", "IMPORT_RECEIPTS")
        qualification = _load_bound(self.qualification_receipt)
        dynamic = _load_bound(self.dynamic_data_receipt)
        representative = _load_json(self.representative_receipt)
        coverage = _load_json(self.static_coverage_receipt)
        source_findings = _scan_dynamic_execution(self.source_roots)
        secret_findings = _scan_secrets(
            (self.qualification_receipt.parent, self.dynamic_data_receipt.parent)
        )
        route_findings = _route_findings()
        self._write_progress("IN_PROGRESS", "EVALUATE_GATES")

        gates = {
            "static_catalog": {
                "passed": coverage.get("level1_static_status") == "PASSED"
                and coverage.get("question_status_counts", {}).get("COVERED_NOT_RUN") == 500,
                "parsed_and_routable": 500,
                "real_execution_claimed": False,
            },
            "representative_families": {
                "passed": representative.get("status") == "PASSED"
                and representative.get("passed_count") == 25,
                "passed_count": representative.get("passed_count", 0),
                "receipt_file_hash": _file_hash(self.representative_receipt),
            },
            "specialist_qualification": {
                "passed": qualification.get("status") == "QUALIFIED_FOR_RESEARCH_PRODUCT",
                "official_count": qualification.get("official_research_qualified_count", 0),
                "auxiliary_count": qualification.get("auxiliary_research_qualified_count", 0),
                "blocked_groups": qualification.get("blocked_groups", []),
                "receipt_hash": qualification.get("artifact_hash"),
            },
            "dynamic_tool_data": {
                "passed": dynamic.get("status") in {"PASSED", "PARTIAL"}
                and dynamic.get("authority_ceiling") == "AUXILIARY_ONLY"
                and dynamic.get("recovery_cursor") == "COMPLETE",
                "status": dynamic.get("status"),
                "authority_ceiling": dynamic.get("authority_ceiling"),
                "document_count": dynamic.get("document_count", 0),
                "failure_codes": dynamic.get("failure_codes", []),
                "receipt_hash": dynamic.get("artifact_hash"),
            },
            "security": {
                "passed": not source_findings and not secret_findings and not route_findings,
                "dynamic_execution_findings": source_findings,
                "secret_findings_count": len(secret_findings),
                "forbidden_routes": route_findings,
            },
            "tests": {
                "passed": self.tests_passed >= 192,
                "passed_count": self.tests_passed,
                "command": self.test_command,
            },
        }
        all_passed = all(bool(item["passed"]) for item in gates.values())
        limitations = []
        if dynamic.get("temporally_covered_symbol_count", 0) == 0:
            limitations.append(
                "当前真实新闻/研报工具结果晚于冻结历史 Evidence 窗口；已冻结为 AUXILIARY，未回填为官方历史特征。"
            )
        limitations.append("500 题已静态解析和路由；真实执行资格由 25 个代表家族与六专业组收据证明，并非声称逐题跑完。")
        payload: dict[str, Any] = {
            "schema_version": "alphaforge.product-finalization-harness/v1",
            "status": (
                "QUALIFIED_FOR_RESEARCH_PRODUCT_WITH_AUXILIARY_LIMITATION"
                if all_passed else "BLOCKED"
            ),
            "gates": gates,
            "limitations": limitations,
            "trusted_path": [
                "Vibe AgentLoop",
                "filtered read-only Vibe ToolRegistry",
                "frozen/PIT data identity",
                "mechanical review",
                "OS-sandboxed experiment",
                "independent Evidence Service",
                "deterministic Chinese report",
            ],
            "recovery_cursor": "COMPLETE" if all_passed else "FIX_FAILED_GATE",
        }
        payload["artifact_hash"] = canonical_hash(payload)
        _atomic_json(self.output_root / "final_verdict.json", payload)
        _atomic_json(self.output_root / "test_receipt.json", gates["tests"])
        _append_event_once(self.output_root / "events.jsonl", {
            "event": "PRODUCT_FINALIZED" if all_passed else "PRODUCT_BLOCKED",
            "verdict_hash": payload["artifact_hash"],
            "status": payload["status"],
        })
        self._write_progress("PASSED" if all_passed else "BLOCKED", payload["recovery_cursor"])
        return payload

    def _load_final(self) -> Mapping[str, Any] | None:
        path = self.output_root / "final_verdict.json"
        if not path.is_file():
            return None
        raw = _load_json(path)
        claimed = raw.get("artifact_hash")
        payload = {key: value for key, value in raw.items() if key != "artifact_hash"}
        if claimed != canonical_hash(payload) or raw.get("recovery_cursor") != "COMPLETE":
            return None
        tests = raw.get("gates", {}).get("tests", {})
        if tests.get("passed_count") != self.tests_passed or tests.get("command") != self.test_command:
            return None
        return raw

    def _write_progress(self, status: str, cursor: str) -> None:
        payload = {
            "schema_version": "alphaforge.product-finalization-progress/v1",
            "status": status,
            "recovery_cursor": cursor,
        }
        payload["artifact_hash"] = canonical_hash(payload)
        _atomic_json(self.output_root / "progress.json", payload)


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"receipt is not an object: {path.name}")
    return value


def _load_bound(path: Path) -> dict[str, Any]:
    value = _load_json(path)
    claimed = value.get("artifact_hash")
    payload = {key: item for key, item in value.items() if key != "artifact_hash"}
    if claimed != canonical_hash(payload):
        raise ValueError(f"receipt identity mismatch: {path.name}")
    return value


def _scan_dynamic_execution(roots: tuple[Path, ...]) -> list[str]:
    findings: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                findings.append(f"{root.name}:UNPARSABLE_SOURCE")
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                    findings.append(f"{root.name}/{path.relative_to(root).as_posix()}:{node.lineno}:{node.func.id}")
    return findings


def _scan_secrets(roots: tuple[Path, ...]) -> list[str]:
    findings: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".json", ".jsonl", ".md", ".txt"}:
                continue
            try:
                if _SECRET.search(path.read_text(encoding="utf-8", errors="ignore")):
                    findings.append(_file_hash(path))
            except OSError:
                findings.append("UNREADABLE_ARTIFACT")
    return findings


def _route_findings() -> list[str]:
    try:
        from alphaforge_api_server import app
    except Exception as exc:
        return [f"API_IMPORT_{type(exc).__name__.upper()}"]
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        for child in getattr(route, "routes", ()):
            child_path = getattr(child, "path", None)
            if isinstance(child_path, str):
                paths.add(child_path)
    return sorted(path for path in paths if any(token in path.casefold() for token in _FORBIDDEN_ROUTES))


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent)
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


def _append_event_once(path: Path, event: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event_id = canonical_hash(event)
    encoded = json.dumps({**event, "event_id": event_id}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    if any(event_id in line for line in existing):
        return
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finalize AlphaForge research product receipts.")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--qualification-receipt", type=Path, required=True)
    parser.add_argument("--dynamic-data-receipt", type=Path, required=True)
    parser.add_argument("--representative-receipt", type=Path, required=True)
    parser.add_argument("--static-coverage-receipt", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, action="append", required=True)
    parser.add_argument("--tests-passed", type=int, required=True)
    parser.add_argument("--test-command", required=True)
    args = parser.parse_args(argv)
    result = ProductFinalizationHarness(
        output_root=args.output_root,
        qualification_receipt=args.qualification_receipt,
        dynamic_data_receipt=args.dynamic_data_receipt,
        representative_receipt=args.representative_receipt,
        static_coverage_receipt=args.static_coverage_receipt,
        source_roots=tuple(args.source_root),
        tests_passed=args.tests_passed,
        test_command=args.test_command,
    ).run()
    print(json.dumps(dict(result), ensure_ascii=False, sort_keys=True))
    return 0 if str(result["status"]).startswith("QUALIFIED") else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ProductFinalizationHarness"]
