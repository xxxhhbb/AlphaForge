from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..data import canonical_content_hash
from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_os_v3.product_service import ResearchOSV3ProductService
from ..research_run_controller import ResearchRunLifecycle
from ..specialist_research import DeterministicSpecialistProvider


DEFAULT_SCENARIOS = (
    "挖掘过去50日动量对未来5日收益的预测能力",
    "研究过去20日反转因子是否仍然有效",
    "盈利质量和 ROE 是否能预测未来收益",
    "成交量放大后的价格趋势是否有延续性",
    "寻找与已有动量低相关的补充 Alpha",
    "诊断最近动量因子为什么失效",
)


@dataclass(frozen=True, slots=True)
class ProductStabilityScenario:
    question: str
    status: str
    run_id: str | None
    selected_specialists: tuple[str, ...]
    candidate_count: int
    accepted_preflight_count: int
    official_evidence_hash: str | None
    report_hash: str | None
    report_path: str | None
    failure_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProductStabilityFinding:
    code: str
    message: str
    severity: str = "ERROR"


@dataclass(frozen=True, slots=True)
class ProductStabilityReport:
    status: str
    scenarios: tuple[ProductStabilityScenario, ...]
    findings: tuple[ProductStabilityFinding, ...]
    product_root: str
    data_root: str

    @property
    def report_hash(self) -> str:
        return canonical_content_hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": "alphaforge.product-stability-harness/v1",
            "status": self.status,
            "scenarios": [asdict(item) for item in self.scenarios],
            "findings": [asdict(item) for item in self.findings],
            "product_root": self.product_root,
            "data_root": self.data_root,
        }
        payload["artifact_hash"] = canonical_content_hash(payload)
        return payload


class ProductStabilityHarness:
    """Verify the UI product spine without creating another research runtime."""

    def __init__(
        self,
        *,
        output_root: str | Path,
        data_root: str | Path,
        dotenv_path: str | Path,
        provider_factory: Callable[[str], object] | None = None,
        questions: Iterable[str] = DEFAULT_SCENARIOS,
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.data_root = Path(data_root).resolve()
        self.dotenv_path = Path(dotenv_path).resolve()
        self.provider_factory = provider_factory or (lambda specialist_id: DeterministicSpecialistProvider(specialist_id))
        self.questions = tuple(questions)

    def run(self) -> ProductStabilityReport:
        findings: list[ProductStabilityFinding] = []
        if not self.questions:
            findings.append(ProductStabilityFinding("NO_SCENARIOS", "product harness needs at least one scenario"))
        repository = FrozenValidationDataRepository(self.data_root)
        if repository.load(require_strict_provenance=True) is None:
            findings.append(ProductStabilityFinding("DATA_UNAVAILABLE", "strict frozen data root is not loadable"))
            return ProductStabilityReport("FAILED", (), tuple(findings), str(self.output_root), str(self.data_root))
        service = ResearchOSV3ProductService(
            root=self.output_root,
            data_loader=lambda: repository.load(require_strict_provenance=True),
            dotenv_path=self.dotenv_path,
            provider_factory=self.provider_factory,
        )
        scenarios: list[ProductStabilityScenario] = []
        try:
            for question in self.questions:
                scenarios.append(self._run_one(service, question, findings))
        finally:
            service.control.close()
        self._check_routes(findings)
        self._check_reports_are_customer_readable(scenarios, findings)
        status = "PASSED" if not any(item.severity == "ERROR" for item in findings) else "FAILED"
        return ProductStabilityReport(status, tuple(scenarios), tuple(findings), str(self.output_root), str(self.data_root))

    def _run_one(
        self,
        service: ResearchOSV3ProductService,
        question: str,
        findings: list[ProductStabilityFinding],
    ) -> ProductStabilityScenario:
        try:
            plan = service.create_plan({"question": question, "depth": "标准研究", "interactionStyle": "标准式"})
            confirmed = service.confirm_plan(str(plan["campaignId"]))
            manifest = service.start(str(confirmed["campaignId"]), seed=7)
            terminal = service.control.wait(manifest.run_id, timeout=120)
            result = service.load_result(terminal.run_id) if terminal.report_hash else None
            report_path = service.report_root / terminal.run_id / "report.md"
            scenario = ProductStabilityScenario(
                question=question,
                status=terminal.current_stage.value,
                run_id=terminal.run_id,
                selected_specialists=terminal.selected_group_ids,
                candidate_count=terminal.candidate_count,
                accepted_preflight_count=terminal.candidate_count if result is None else result.accepted_preflight_count,
                official_evidence_hash=terminal.official_evidence_hash,
                report_hash=terminal.report_hash,
                report_path=str(report_path) if report_path.exists() else None,
                failure_codes=terminal.typed_failures,
            )
        except Exception as exc:
            code = getattr(getattr(exc, "code", None), "value", None) or type(exc).__name__
            findings.append(ProductStabilityFinding("SCENARIO_EXCEPTION", f"{question}: {code}"))
            return ProductStabilityScenario(question, "SYSTEM_FAILED", None, (), 0, 0, None, None, None, (type(exc).__name__,))
        if terminal.current_stage not in {ResearchRunLifecycle.COMPLETED, ResearchRunLifecycle.PARTIAL, ResearchRunLifecycle.BLOCKED}:
            findings.append(ProductStabilityFinding("ILLEGAL_TERMINAL_STATE", f"{question}: {terminal.current_stage.value}"))
        if scenario.report_hash is None or scenario.report_path is None:
            findings.append(ProductStabilityFinding("REPORT_MISSING", f"{question}: terminal run has no published report"))
        if not 1 <= len(scenario.selected_specialists) <= 6:
            findings.append(ProductStabilityFinding("SPECIALIST_SELECTION_INVALID", f"{question}: selected specialist count is invalid"))
        if scenario.status in {"COMPLETED", "PARTIAL"} and scenario.candidate_count <= 0:
            findings.append(ProductStabilityFinding("NO_CANDIDATES", f"{question}: completed run has no candidates"))
        return scenario

    @staticmethod
    def _check_routes(findings: list[ProductStabilityFinding]) -> None:
        try:
            from alphaforge_api_server import app
            from src.api.research_os_v3_routes import research_os_v3_control_route_paths
        except Exception as exc:
            findings.append(ProductStabilityFinding("API_APP_UNAVAILABLE", type(exc).__name__))
            return
        paths: list[str] = []
        for route in app.routes:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.append(path)
            for child in getattr(route, "routes", ()):
                child_path = getattr(child, "path", None)
                if isinstance(child_path, str):
                    paths.append(child_path)
        paths = sorted(set(paths))
        forbidden = [path for path in paths if any(token in path.lower() for token in ("/live", "broker", "order", "oauth", "mandate", "/research-os/v2"))]
        if forbidden:
            findings.append(ProductStabilityFinding("FORBIDDEN_PRODUCT_ROUTE", ", ".join(forbidden[:8])))
        control_paths = set(research_os_v3_control_route_paths())
        if not (
            any(path == "/api/veriquant/research-os/v3/plans" for path in paths)
            or "/api/veriquant/research-os/v3/plans" in control_paths
            or "/plans" in control_paths
        ):
            findings.append(ProductStabilityFinding("V3_PLAN_ROUTE_MISSING", "V3 product plan endpoint is not registered"))

    @staticmethod
    def _check_reports_are_customer_readable(
        scenarios: tuple[ProductStabilityScenario, ...] | list[ProductStabilityScenario],
        findings: list[ProductStabilityFinding],
    ) -> None:
        forbidden_terms = ("Desk", "Router", "Swarm", "pipeline", "Pipeline")
        # This spine renders the campaign-level report (not the single
        # specialist CustomerResearchReport).  Require the current, explicit
        # machine-results and reproduction sections instead of a retired
        # combined heading so report drift fails for substantive omissions.
        required_sections = (
            "一页结论",
            "为什么得到这个结论",
            "机器结果总览",
            "候选明细",
            "审计附录",
        )
        for scenario in scenarios:
            if not scenario.report_path:
                continue
            path = Path(scenario.report_path)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                findings.append(ProductStabilityFinding("REPORT_UNREADABLE", scenario.question))
                continue
            for section in required_sections:
                if section not in text:
                    findings.append(ProductStabilityFinding("REPORT_SECTION_MISSING", f"{scenario.question}: {section}"))
            leaks = [term for term in forbidden_terms if term in text]
            if leaks:
                findings.append(ProductStabilityFinding("INTERNAL_TERM_LEAK", f"{scenario.question}: {', '.join(leaks)}"))


__all__ = [
    "DEFAULT_SCENARIOS",
    "ProductStabilityFinding",
    "ProductStabilityHarness",
    "ProductStabilityReport",
    "ProductStabilityScenario",
]
