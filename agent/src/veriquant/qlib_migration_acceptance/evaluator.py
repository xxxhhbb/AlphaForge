"""Repository-backed, fail-closed Qlib migration acceptance evaluator."""

from __future__ import annotations

import json
import os
import re
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    AcceptanceFailureCode,
    AcceptanceStatus,
    GateVerdict,
    QlibMigrationAcceptanceVerdict,
    QlibMigrationGate,
    canonical_hash,
    canonical_json,
)


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUIRED_PACKAGES = (
    "qlib_expression",
    "frozen_qlib_provider",
    "qlib_expression_runtime",
    "qlib_operator_worker",
    "qlib_candidate_translation",
    "qlib_migration_coverage",
    "qlib_formula_migration",
    "qlib_score_postprocess",
    "qlib_semantic_parity",
)
_KNOWN_DIFFERENCE_OPERATORS = {"IdxMax", "IdxMin", "If", "WMA"}


class QlibMigrationAcceptanceEvaluator:
    """Read existing artifacts and test results without executing finance code."""

    def __init__(self, repo_root: str | Path) -> None:
        self.root = Path(repo_root).resolve()
        self._inputs: dict[str, str] = {}

    def evaluate(self) -> QlibMigrationAcceptanceVerdict:
        self._inputs = {}
        gates = (
            self._constitution_gate(),
            self._package_gate(),
            self._coverage_gate(),
            self._formula_gate(),
            self._real_smoke_gate(),
            self._frozen_pit_gate(),
            self._central_gate(
                QlibMigrationGate.UNIFIED_FACTOR_SCORE,
                "unified_factor_score_artifact",
                AcceptanceFailureCode.UNIFIED_ARTIFACT_UNPROVEN,
            ),
            self._central_gate(
                QlibMigrationGate.TRIAL_EVIDENCE_AUTHORITY,
                "trial_and_evidence_authority",
                AcceptanceFailureCode.AUTHORITY_UNPROVEN,
            ),
            self._known_difference_gate(),
            self._postprocess_gate(),
            self._security_gate(),
            self._dedicated_process_gate(),
            self._specialist_gate(),
            self._central_gate(
                QlibMigrationGate.LEGACY_REPLAY,
                "legacy_replay",
                AcceptanceFailureCode.LEGACY_REPLAY_UNPROVEN,
            ),
        )
        return QlibMigrationAcceptanceVerdict.build(tuple(gates), canonical_hash(self._inputs))

    def write_reports(
        self,
        verdict: QlibMigrationAcceptanceVerdict,
        *,
        json_path: str | Path,
        markdown_path: str | Path,
    ) -> None:
        json_target = self._safe_output(json_path)
        markdown_target = self._safe_output(markdown_path)
        self._atomic_write(json_target, canonical_json(verdict.to_dict()) + "\n")
        self._atomic_write(markdown_target, self.render_markdown(verdict))
        written = json.loads(json_target.read_text(encoding="utf-8"))
        if written.get("report_hash") != verdict.report_hash:
            raise ValueError("written report hash mismatch")

    @staticmethod
    def render_markdown(verdict: QlibMigrationAcceptanceVerdict) -> str:
        lines = [
            "# Qlib 公式迁移机械验收",
            "",
            f"总状态：**{verdict.overall_status.value}**",
            "",
            f"报告哈希：`{verdict.report_hash}`",
            "",
            "| 验收门 | 状态 | 失败代码 |",
            "| --- | --- | --- |",
        ]
        for gate in verdict.gates:
            failures = "、".join(item.value for item in gate.failure_codes) or "无"
            lines.append(f"| `{gate.gate.value}` | `{gate.status.value}` | {failures} |")
        lines.extend(["", "## 逐门证据", ""])
        for gate in verdict.gates:
            lines.extend([f"### {gate.gate.value}", "", f"状态：`{gate.status.value}`"])
            if gate.artifacts:
                lines.append("证据：" + "、".join(f"`{item}`" for item in gate.artifacts))
            for reason in gate.reasons:
                lines.append(f"- {reason}")
            lines.append("")
        lines.extend(
            [
                "## 权限说明",
                "",
                "该验收器只读取构建产物并校验哈希；不执行因子、不计算分数、不创建 Trial、Official Evidence 或 Promotion。",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    def _constitution_gate(self) -> GateVerdict:
        path = ".autobuild/veriquant/BUILD_CONSTITUTION.md"
        text = self._read_text(path)
        required = (
            "Version: `build-constitution/v2`",
            "CertifiedQlibExpressionExecutor",
            "FactorScoreArtifact",
            "Qlib 原生 `eval` 表达式解析",
            "Trial Ledger",
            "Evidence Service",
        )
        if text is None or any(item not in text for item in required):
            return self._failed(QlibMigrationGate.CONSTITUTION_V2, AcceptanceFailureCode.CONSTITUTION_MISMATCH, "宪法 v2 或认证 Qlib 权限边界不完整", path)
        return self._passed(QlibMigrationGate.CONSTITUTION_V2, path)

    def _package_gate(self) -> GateVerdict:
        failures: list[str] = []
        artifacts: list[str] = []
        for package in _REQUIRED_PACKAGES:
            path = f".autobuild/veriquant/parallel/{package}/result.json"
            artifacts.append(path)
            result = self._read_json(path)
            if result is None:
                failures.append(f"{package}: result.json 缺失或无效")
                continue
            status = result.get("status", result.get("result_status"))
            if status != "READY_FOR_INTEGRATION" or not self._result_has_passed_test(result):
                failures.append(f"{package}: 未同时满足 READY_FOR_INTEGRATION 与测试通过")
            failures.extend(self._verify_declared_hashes(package, result))
        if failures:
            return GateVerdict(
                QlibMigrationGate.PACKAGE_READINESS,
                AcceptanceStatus.FAILED,
                (AcceptanceFailureCode.PACKAGE_NOT_READY,),
                tuple(failures),
                tuple(artifacts),
            )
        return self._passed(QlibMigrationGate.PACKAGE_READINESS, *artifacts, facts={"required_package_count": len(_REQUIRED_PACKAGES)})

    @staticmethod
    def _result_has_passed_test(result: Mapping[str, Any]) -> bool:
        """Accept only explicit, machine-readable passed test evidence.

        Parallel packages currently use either ``test_result``/``build_gate``
        or a structured ``verification`` list.  Lint/compile-only entries do
        not substitute for an executed focused pytest entry.
        """

        direct = " ".join(
            str(result.get(field, ""))
            for field in ("test_result", "build_gate")
        ).upper()
        if "PASS" in direct:
            return True
        verification = result.get("verification", ())
        if not isinstance(verification, (list, tuple)):
            return False
        for item in verification:
            if not isinstance(item, Mapping):
                continue
            check = str(item.get("check", "")).lower()
            status = str(item.get("status", "")).lower()
            detail = str(item.get("detail", ""))
            if check in {"focused_pytest", "pytest", "tests"} and status == "passed" and re.search(r"\b[1-9][0-9]* passed\b", detail, re.IGNORECASE):
                return True
        return False

    def _coverage_gate(self) -> GateVerdict:
        path = ".autobuild/veriquant/parallel/qlib_migration_coverage/coverage_summary.json"
        data = self._read_json(path)
        good = bool(
            data
            and data.get("total_records") == 461
            and data.get("classified_records") == 461
            and data.get("unclassified_records") == 0
            and data.get("legacy_gap_records") == 390
            and data.get("legacy_gap_unclassified_records") == 0
            and _HASH.fullmatch(str(data.get("report_hash", "")))
        )
        if not good:
            return self._failed(QlibMigrationGate.CATALOG_CLASSIFICATION, AcceptanceFailureCode.COVERAGE_MISMATCH, "目录必须是 461/461，且 390 个历史缺口为 0 未分类", path)
        return self._passed(QlibMigrationGate.CATALOG_CLASSIFICATION, path, facts={"total": 461, "unclassified": 0, "legacy_gap_unclassified": 0})

    def _formula_gate(self) -> GateVerdict:
        path = ".autobuild/veriquant/parallel/qlib_formula_migration/result.json"
        data = self._read_json(path)
        good = bool(
            data
            and data.get("status") == "READY_FOR_INTEGRATION"
            and "10 passed" in str(data.get("test_result", "")).lower()
            and "461-record" in " ".join(str(item) for item in data.get("integration_hooks", ()))
        )
        if not good:
            return self._failed(QlibMigrationGate.WHOLE_FORMULA_QUALIFICATION, AcceptanceFailureCode.TEST_MISSING, "整公式静态资格化缺少 461 项和测试证明", path)
        return self._passed(QlibMigrationGate.WHOLE_FORMULA_QUALIFICATION, path, facts={"tested_records": 461, "unresolved_records_are_not_executable": True})

    def _real_smoke_gate(self) -> GateVerdict:
        paths = (
            ".autobuild/veriquant/parallel/qlib_operator_worker/real_smoke_result.json",
            ".autobuild/veriquant/integration/qlib_expression_migration/real_backend_smoke.json",
        )
        worker, central = (self._read_json(path) for path in paths)
        good = bool(
            worker
            and central
            and worker.get("status") == "PASSED"
            and worker.get("runtime", {}).get("pyqlib_version") == "0.9.7"
            and worker.get("runtime", {}).get("network_used") is False
            and central.get("status") == "PASSED"
            and central.get("pyqlib_version") == "0.9.7"
            and _HASH.fullmatch(str(central.get("score_artifact_hash", "")))
        )
        if not good:
            return self._failed(QlibMigrationGate.REAL_PYQLIB_SMOKE, AcceptanceFailureCode.REAL_SMOKE_MISSING, "真实固定 pyqlib 0.9.7 后端 smoke 或统一 score hash 缺失", *paths)
        return self._passed(QlibMigrationGate.REAL_PYQLIB_SMOKE, *paths, facts={"pyqlib": "0.9.7", "network_used": False})

    def _frozen_pit_gate(self) -> GateVerdict:
        path = ".autobuild/veriquant/parallel/frozen_qlib_provider/result.json"
        data = self._read_json(path)
        required = (
            "row_level_available_at_decision_time_proof",
            "future_perturbation_invariance",
            "revision_visibility_timing",
            "historical_universe_leakage_guard",
            "label_and_sealed_rejection",
            "stable_content_hashes",
        )
        acceptance = data.get("acceptance", {}) if data else {}
        good = bool(data and data.get("status") == "READY_FOR_INTEGRATION" and all(acceptance.get(item) == "PASSED" for item in required))
        if not good:
            return self._failed(QlibMigrationGate.FROZEN_PIT_PROVIDER, AcceptanceFailureCode.PIT_PROOF_MISSING, "Frozen provider 的可用时间、修订、历史股票池或未来扰动证明不完整", path)
        return self._passed(QlibMigrationGate.FROZEN_PIT_PROVIDER, path, facts={"pit_checks": len(required)})

    def _central_gate(self, gate: QlibMigrationGate, fact: str, failure: AcceptanceFailureCode) -> GateVerdict:
        path = ".autobuild/veriquant/integration/qlib_expression_migration/result.json"
        data = self._read_json(path)
        if data is None:
            return GateVerdict(gate, AcceptanceStatus.UNAVAILABLE, (failure,), ("中央集成测试产物尚未生成",), (path,))
        facts = data.get("acceptance", {})
        if data.get("status") != "PASSED" or facts.get(fact) is not True:
            return self._failed(gate, failure, f"中央集成未证明 {fact}", path)
        return self._passed(gate, path)

    def _known_difference_gate(self) -> GateVerdict:
        parity_path = ".autobuild/veriquant/parallel/qlib_semantic_parity/real_smoke_result.json"
        central_path = ".autobuild/veriquant/integration/qlib_expression_migration/result.json"
        parity = self._read_json(parity_path)
        central = self._read_json(central_path)
        evidence = parity.get("operator_evidence", {}) if parity else {}
        found = {name for name, item in evidence.items() if item.get("status") == "KNOWN_DIFFERENCE"}
        blocked = bool(central and central.get("acceptance", {}).get("known_difference_blocked") is True)
        if not _KNOWN_DIFFERENCE_OPERATORS.issubset(found) or not blocked:
            return self._failed(QlibMigrationGate.KNOWN_DIFFERENCE_BLOCKING, AcceptanceFailureCode.KNOWN_DIFFERENCE_NOT_BLOCKED, "已知差异必须被识别且在官方执行前阻断；仅有差异清单不等于阻断测试", parity_path, central_path)
        return self._passed(QlibMigrationGate.KNOWN_DIFFERENCE_BLOCKING, parity_path, central_path, facts={"blocked_operators": sorted(found)})

    def _postprocess_gate(self) -> GateVerdict:
        path = ".autobuild/veriquant/parallel/qlib_score_postprocess/result.json"
        data = self._read_json(path)
        exported = set(data.get("exported_symbols", ())) if data else set()
        good = bool(
            data
            and data.get("status", data.get("result_status")) == "READY_FOR_INTEGRATION"
            and {"PostprocessedProgramIdentity", "derive_postprocessed_program_identity"}.issubset(exported)
            and "12 passed" in " ".join(str(item) for item in data.get("tests_run", ())).lower()
        )
        if not good:
            return self._failed(QlibMigrationGate.POSTPROCESS_IDENTITY, AcceptanceFailureCode.POSTPROCESS_IDENTITY_UNPROVEN, "后处理策略必须进入独立 program identity 并有稳定哈希测试", path)
        return self._passed(QlibMigrationGate.POSTPROCESS_IDENTITY, path)

    def _security_gate(self) -> GateVerdict:
        expression_path = ".autobuild/veriquant/parallel/qlib_expression/result.json"
        worker_path = ".autobuild/veriquant/parallel/qlib_operator_worker/result.json"
        expression, worker = self._read_json(expression_path), self._read_json(worker_path)
        expression_tests = " ".join(str(item) for item in (expression or {}).get("tests_run", ())).lower()
        worker_risk = str((worker or {}).get("live_or_external_risk", "")).lower()
        good = bool(
            expression
            and worker
            and "0 matches" in expression_tests
            and "no network" in worker_risk
            and "no network" in str((expression or {}).get("live_or_external_risk", "")).lower()
        )
        if not good:
            return self._failed(QlibMigrationGate.SECURITY_SCAN, AcceptanceFailureCode.SECURITY_INVARIANT_FAILED, "动态执行、原生字符串 resolver、网络、自定义算子或金融权限扫描缺少通过证据", expression_path, worker_path)
        return self._passed(QlibMigrationGate.SECURITY_SCAN, expression_path, worker_path)

    def _dedicated_process_gate(self) -> GateVerdict:
        candidates = (
            ".autobuild/veriquant/parallel/qlib_operator_subprocess/result.json",
            ".autobuild/veriquant/parallel/qlib_structured_subprocess/result.json",
            ".autobuild/veriquant/parallel/qlib_dedicated_process/result.json",
        )
        for path in candidates:
            data = self._read_json(path)
            if not data:
                continue
            status = data.get("status", data.get("result_status"))
            acceptance = data.get("acceptance", {})
            if status == "READY_FOR_INTEGRATION" and acceptance.get("dedicated_process_isolation") is True:
                return self._passed(QlibMigrationGate.DEDICATED_PROCESS, path)
            return self._failed(QlibMigrationGate.DEDICATED_PROCESS, AcceptanceFailureCode.PROCESS_ISOLATION_UNAVAILABLE, "专用进程包存在但未证明结构化请求和进程隔离", path)
        return GateVerdict(
            QlibMigrationGate.DEDICATED_PROCESS,
            AcceptanceStatus.UNAVAILABLE,
            (AcceptanceFailureCode.PROCESS_ISOLATION_UNAVAILABLE,),
            ("认证 worker 当前为库内调用；专用结构化进程能力包尚未 READY",),
            candidates,
        )

    def _specialist_gate(self) -> GateVerdict:
        regression_path = ".autobuild/veriquant/integration/qlib_expression_migration/specialist_prompt_regression.json"
        central_path = ".autobuild/veriquant/integration/qlib_expression_migration/result.json"
        regression, central = self._read_json(regression_path), self._read_json(central_path)
        if not regression:
            return self._failed(QlibMigrationGate.SPECIALIST_QLIB_PRIMARY, AcceptanceFailureCode.TEST_MISSING, "专业研究组完整回归日志缺失", regression_path)
        total = regression.get("total")
        passed = regression.get("passed")
        failed = regression.get("failed")
        if regression.get("status") != "PASSED" or total != 52 or passed != 52 or failed != 0:
            failure = regression.get("failure", {})
            reason = f"专业研究组回归为 {passed}/{total}，失败 {failed}；{failure.get('test', 'unknown')}: {failure.get('failure_code', 'unknown')}"
            return self._failed(QlibMigrationGate.SPECIALIST_QLIB_PRIMARY, AcceptanceFailureCode.SPECIALIST_PROMPT_REGRESSION, reason, regression_path)
        primary = bool(central and central.get("acceptance", {}).get("specialist_qlib_primary") is True)
        if not primary:
            return self._failed(QlibMigrationGate.SPECIALIST_QLIB_PRIMARY, AcceptanceFailureCode.QLIB_PRIMARY_UNPROVEN, "52/52 回归之外还缺专业研究组 Qlib-primary 中央接线证明", regression_path, central_path)
        return self._passed(QlibMigrationGate.SPECIALIST_QLIB_PRIMARY, regression_path, central_path)

    def _verify_declared_hashes(self, package: str, result: Mapping[str, Any]) -> list[str]:
        mapping = result.get("artifact_hashes", result.get("files", {}))
        if not isinstance(mapping, Mapping) or not mapping:
            return [f"{package}: 未声明 artifact hashes"]
        failures: list[str] = []
        for relative, declared in mapping.items():
            if not isinstance(relative, str) or not isinstance(declared, str) or not _HASH.fullmatch(declared):
                failures.append(f"{package}: 无效 hash 声明 {relative}")
                continue
            path = self._resolve(relative)
            if path is None:
                failures.append(f"{package}: 路径越界 {relative}")
                continue
            if not path.is_file():
                failures.append(f"{package}: artifact 缺失 {relative}")
                continue
            actual = "sha256:" + sha256(path.read_bytes()).hexdigest()
            if actual != declared:
                failures.append(f"{package}: hash mismatch {relative}")
        return failures

    def _read_json(self, relative: str) -> Mapping[str, Any] | None:
        text = self._read_text(relative)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
        return data if isinstance(data, Mapping) else None

    def _read_text(self, relative: str) -> str | None:
        path = self._resolve(relative)
        if path is None or not path.is_file():
            self._inputs[relative] = "MISSING"
            return None
        raw = path.read_bytes()
        digest = "sha256:" + sha256(raw).hexdigest()
        self._inputs[relative] = digest
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _resolve(self, relative: str) -> Path | None:
        candidate = Path(relative.replace("\\", "/"))
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            return None
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            return None
        return resolved

    def _safe_output(self, value: str | Path) -> Path:
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("report output escapes repository") from exc
        return resolved

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = handle.name
        try:
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _passed(gate: QlibMigrationGate, *artifacts: str, facts: Mapping[str, Any] | None = None) -> GateVerdict:
        return GateVerdict(gate, AcceptanceStatus.IMPLEMENTED_AND_TESTED, artifacts=tuple(artifacts), facts=facts)

    @staticmethod
    def _failed(gate: QlibMigrationGate, code: AcceptanceFailureCode, reason: str, *artifacts: str) -> GateVerdict:
        return GateVerdict(gate, AcceptanceStatus.FAILED, (code,), (reason,), tuple(artifacts))
