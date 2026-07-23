"""Offline upgrade of verified pre-v3 product receipts to ResearchAnswer v3.

This module never calls a provider, runs source code, or recalculates metrics.
It accepts only a locally complete product run, verifies its persisted content
identities, and regenerates the deterministic answer/report presentation layer
in a sibling directory.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any

from ..agent_research_product.collaboration import EvidenceCollaborationAggregator
from ..agent_research_product.contracts import (
    ProductExperimentResult,
    ProductRunRequest,
    ResearchResultBundle,
)
from ..agent_research_product.service import MinimalChineseReportBuilder
from ..agent_research_sandbox.process_runner import canonical_hash
from ..agent_research_sandbox.report import (
    ChineseResearchReportComposer,
    CustomerResearchReport,
)
from ..research_agent_runtime import ResearchAgentTask
from ..research_answer import ResearchAnswerAssembler
from ..research_answer.compiler import ResearchAnswerContractCompiler


class LegacyProductRunBackfiller:
    """Build v3 answer artifacts from already verified local run receipts."""

    def upgrade(self, product_result_path: str | Path) -> dict[str, Any]:
        result_path = Path(product_result_path).resolve()
        source_root = result_path.parent
        product_raw = _read_json(result_path)
        legacy_report_raw = _read_json(source_root / "report.json")
        legacy_report = CustomerResearchReport.from_dict(legacy_report_raw)
        if product_raw.get("report_hash") != legacy_report.artifact_hash:
            raise ValueError("legacy product result and report identity disagree")

        manifest_raw = _read_json(source_root / "manifest.json")
        manifest_hash = canonical_hash(manifest_raw)
        if product_raw.get("manifest_hash") != manifest_hash:
            raise ValueError("legacy product result and manifest identity disagree")
        bundle_raw = _read_json(source_root / "research_result_bundle.json")
        bundle = ResearchResultBundle.from_dict(bundle_raw)
        if bundle_raw.get("artifact_hash", bundle.artifact_hash) != bundle.artifact_hash:
            raise ValueError("legacy result bundle identity disagree")

        experiments = tuple(
            self._verified_experiment(source_root, ProductExperimentResult.from_dict(item))
            for item in product_raw.get("experiments", ())
        )
        output_by_specialist = self._output_by_specialist(source_root, experiments)
        decision_time = _decision_time(legacy_report, manifest_raw)
        manifest = SimpleNamespace(
            run_id=str(manifest_raw["run_id"]),
            artifact_hash=manifest_hash,
            data_snapshot_hash=str(manifest_raw["data_snapshot_hash"]),
            campaign_hash=str(manifest_raw["campaign_hash"]),
        )
        selected = tuple(sorted(set(manifest_raw.get("selected_group_ids", ())) | set(output_by_specialist)))
        if not selected:
            raise ValueError("legacy run has no selected specialists")
        semantic_spec = ResearchAnswerContractCompiler().compile(
            question_id=legacy_report.question_id,
            question=legacy_report.question,
            task_hashes=(),
        )
        semantic_ids = tuple(item.requirement_id for item in semantic_spec.requirements)
        semantic_values = tuple(sorted({
            semantic for item in semantic_spec.requirements for semantic in item.dimensions
        }))
        comparison_required = any(item.comparison_required for item in semantic_spec.requirements)
        tasks = tuple(
            ResearchAgentTask(
                task_id=f"backfill_{specialist}",
                run_id=manifest.run_id,
                specialist_id=specialist,
                question=legacy_report.question,
                campaign_hash=manifest.campaign_hash,
                data_snapshot_hash=manifest.data_snapshot_hash,
                decision_time=decision_time,
                allowed_tools=(),
                output_kind=output_by_specialist.get(specialist, "ScoreTable"),
                answer_requirement_ids=semantic_ids,
                required_answer_semantics=semantic_values,
                comparison_required=comparison_required,
                minimum_experiments=2 if comparison_required else 1,
                max_experiments=3,
            )
            for specialist in selected
        )
        request = ProductRunRequest(
            manifest=manifest,
            question_id=legacy_report.question_id,
            question=legacy_report.question,
            tasks=tasks,
            runtimes={},
            toolboxes={},
        )
        source_repository = _LegacyDraftRepository(source_root.parent.parent / "draft_sources")
        assembler = ResearchAnswerAssembler()
        spec = assembler.compile_spec(request)
        candidates = assembler.build_candidates(
            request=request,
            swarm=None,
            experiments=experiments,
            artifact_repository=source_repository,
        )
        legacy_plans = {
            _specialist_id(item.specialist): item.plan
            for item in legacy_report.research_process
            if item.plan.strip()
        }
        candidates = tuple(
            replace(
                item,
                method_summary=legacy_plans.get(item.specialist_id, item.method_summary),
            )
            for item in candidates
        )
        collaboration = EvidenceCollaborationAggregator().summarize_experiments(
            run_id=manifest.run_id,
            experiments=experiments,
        )
        failures = tuple(sorted(set(str(item) for item in product_raw.get("failures", ()))))
        answer = assembler.assemble(
            request=request,
            spec=spec,
            candidates=candidates,
            experiments=experiments,
            result_bundle=bundle,
            collaboration=collaboration,
            failures=failures,
        )
        artifacts = tuple(sorted(set(legacy_report.artifact_hashes) | {
            manifest_hash,
            bundle.artifact_hash,
            spec.artifact_hash,
            collaboration.artifact_hash,
            answer.artifact_hash,
            *(item.artifact_hash for item in candidates),
        }))
        report = MinimalChineseReportBuilder().build(
            request=request,
            swarm=None,
            experiments=experiments,
            failures=failures,
            artifact_hashes=artifacts,
            result_bundle=bundle,
            artifact_repository=None,
            research_answer=answer.to_dict(),
        )
        # The legacy process narrative is itself content addressed and is the
        # only safe source of historical plans/tool calls. Preserve it rather
        # than inventing a process from metric names.
        report = replace(
            report,
            research_process=legacy_report.research_process,
            data_description=legacy_report.data_description,
        )
        markdown = ChineseResearchReportComposer().render(report)

        target = source_root / "research_answer_v3_backfill"
        target.mkdir(parents=True, exist_ok=True)
        _atomic_json(target / "manifest.json", manifest_raw)
        _atomic_json(target / "answer_spec.json", spec.to_dict())
        _atomic_json(target / "candidate_definitions.json", {
            "schema_version": "alphaforge.candidate-definitions/v1",
            "candidates": [item.to_dict() for item in candidates],
        })
        _atomic_json(target / "evidence_collaboration.json", collaboration.to_dict())
        _atomic_json(target / "research_answer.json", answer.to_dict())
        _atomic_json(target / "report.json", report.to_dict())
        _atomic_text(target / "report.md", markdown)
        receipt = {
            "schema_version": "alphaforge.research-answer-v3-backfill-receipt/v1",
            "source_product_result": str(result_path),
            "source_product_result_hash": canonical_hash(product_raw),
            "source_report_hash": legacy_report.artifact_hash,
            "manifest_hash": manifest_hash,
            "execution_mode": "LOCAL_VERIFIED_RECEIPT_REPLAY",
            "provider_calls": 0,
            "runner_calls": 0,
            "answer_hash": answer.artifact_hash,
            "answer_status": answer.answer_status.value,
            "research_verdict": answer.research_verdict.value,
            "report_hash": report.artifact_hash,
            "output_kinds": sorted({item.output_kind for item in candidates}),
            "evidence_authorities": sorted({
                item.status.value for item in experiments
                if item.official_evidence_hash or item.research_evidence_hash
            }),
        }
        receipt["artifact_hash"] = canonical_hash(receipt)
        _atomic_json(target / "backfill_receipt.json", receipt)
        return receipt

    @staticmethod
    def _verified_experiment(
        source_root: Path, experiment: ProductExperimentResult
    ) -> ProductExperimentResult:
        directory = source_root / "experiments" / experiment.experiment_id
        identities = (
            ("review.json", experiment.review_hash),
            ("runner.json", experiment.runner_receipt_hash),
            ("evidence.json", experiment.evidence_result_hash),
            ("official_evidence.json", experiment.official_evidence_hash),
            ("auxiliary_evidence.json", experiment.research_evidence_hash),
        )
        for name, expected in identities:
            if expected is None:
                continue
            path = directory / name
            if not path.is_file():
                raise ValueError(f"legacy experiment identity mismatch: {experiment.experiment_id}/{name}")
            value = _read_json(path)
            # Persisted typed receipts carry their own contract-defined hash;
            # repository index artifacts without that field are addressed by
            # their complete canonical JSON payload.
            actual = value.get("artifact_hash") or canonical_hash(value)
            if actual != expected:
                raise ValueError(f"legacy experiment identity mismatch: {experiment.experiment_id}/{name}")
        table = directory / "standard_table.json"
        output_kind = experiment.output_kind
        if table.is_file():
            value = _read_json(table)
            output_kind = str(value.get("contract_type") or output_kind or "")
        return replace(experiment, output_kind=output_kind or None)

    @staticmethod
    def _output_by_specialist(
        source_root: Path, experiments: tuple[ProductExperimentResult, ...]
    ) -> dict[str, str]:
        values: dict[str, set[str]] = {}
        for item in experiments:
            if item.output_kind:
                values.setdefault(item.specialist_id, set()).add(item.output_kind)
        conflicting = {key: value for key, value in values.items() if len(value) > 1}
        if conflicting:
            raise ValueError("legacy specialist emitted incompatible output contracts")
        return {key: next(iter(value)) for key, value in values.items()}


def _decision_time(report: CustomerResearchReport, manifest: dict[str, Any]) -> datetime:
    for item in report.data_description:
        if item.startswith("决策时间："):
            value = datetime.fromisoformat(item.split("：", 1)[1])
            if value.tzinfo is not None:
                return value
    value = datetime.fromisoformat(str(manifest["created_at"]).replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("legacy decision time is not timezone aware")
    return value


def _specialist_id(value: str) -> str:
    return {
        "另类数据研究组": "alternative_data",
        "预期与事件研究组": "expectations_events",
        "基本面研究组": "fundamental",
        "宏观状态研究组": "macro_regime",
        "市场行为与交易可行性研究组": "market_structure",
        "组合增量研究组": "meta_alpha",
    }.get(value, value)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.name}")
    return value


class _LegacyDraftRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def get(self, content_hash: str) -> str | None:
        if not content_hash.startswith("sha256:"):
            return None
        digest = content_hash.removeprefix("sha256:")
        if len(digest) != 64:
            return None
        path = self.root / digest[:2] / digest[2:]
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8")
        actual = "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
        return value if actual == content_hash else None


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n",
    )


def _atomic_text(path: Path, value: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


__all__ = ["LegacyProductRunBackfiller"]
