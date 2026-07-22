"""Explicit real-provider canary for the single Agent research product path.

The module contains assembly code only.  It reads an already-frozen
``FrozenValidationDataRepository`` supplied by the operator, gives the Agent a
small pathless metadata toolbox, and delegates review, OS isolation and metrics
to their existing independent services.  It never synthesizes a fallback data
set and it never resolves a mutable Docker tag as an execution authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ..agent_research_sandbox import (
    ExperimentReviewGate,
    ExperimentReviewPolicy,
    SandboxAmendmentIdentity,
    ScoreTable,
)
from ..agent_research_sandbox.docker_runner import (
    DockerResearchConfig,
    DockerResearchRunner,
    DockerTrainingObservation,
)
from ..agent_research_sandbox.process_runner import AMENDMENT_VERSION, canonical_hash
from ..data import LabelRow
from ..evidence import CostModel, RegisteredFactor
from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_agent_runtime import (
    BranchCheckpointStore,
    InvestigationReceipt,
    ResearchAgentRuntimeProtocol,
    ResearchAgentTask,
    ResearchAgentOutcome,
    ResearchAgentStatus,
    CompositeResearchToolbox,
    ResearchDraftArtifact,
    ResearchDraftKind,
    ResearchSwarmSchedulerAdapter,
    ResearchToolResult,
    ResearchToolboxProtocol,
    SafeVibeAgentLoopFactory,
    VibeAgentLoopAdapter,
)
from ..research_agent_runtime.real_llm import ConfiguredResearchLLMFactory
from ..research_run_controller import ResearchRunLifecycle, ResearchRunManifest
from ..research_toolbox import (
    ContentAddressedHandleStore,
    VIBE_READONLY_RESEARCH_TOOLS,
    VibeResearchToolbox,
    available_vibe_research_registry,
)
from ..research_data_gateway.dynamic_tool_freeze import load_authority_manifest
from .contracts import ProductRunRequest, ProductRunResult
from .evidence_gateway import UnifiedResearchEvidenceEvaluator
from .production_binding import (
    ExperimentExecutionSpec,
    FrozenFeatureObservation,
    ProductionEvidenceContext,
    ProductionExperimentBinding,
)
from .service import AgentResearchProductService, ContentAddressedDraftSourceRepository


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_LABEL = "APPROVED_PROXY_DATA_VENDOR"
_SCHEMA_VERSION = "alphaforge.agent-research-product-canary/v2"
_AMENDMENT_AUTHORIZED_AT = datetime(2026, 7, 21, tzinfo=timezone.utc)
_SPECIALISTS = frozenset({
    "alternative_data", "expectations_events", "fundamental",
    "macro_regime", "market_structure", "meta_alpha",
})


@dataclass(frozen=True, slots=True)
class ProductCanaryConfiguration:
    data_root: Path
    artifact_root: Path
    question: str
    docker_image: str
    docker_image_id: str
    label_horizon_sessions: int = 20
    seed: int = 7
    max_sessions: int = 80
    max_symbols: int = 20
    specialist_id: str = "market_structure"
    specialist_ids: tuple[str, ...] = ()
    output_kind: str = "ScoreTable"
    specialist_output_kinds: tuple[tuple[str, str], ...] = ()
    dotenv_path: Path = Path("agent/.env")
    resume_source_path: Path | None = None
    resume_source_hash: str | None = None
    resume_sources: tuple[tuple[str, Path, str], ...] = ()
    vibe_tool_names: tuple[str, ...] = ()
    allow_vibe_network_tools: bool = False

    def __post_init__(self) -> None:
        data_root = Path(self.data_root).resolve()
        artifact_root = Path(self.artifact_root).resolve()
        if not (data_root / "frozen_validation_data.json").is_file():
            raise ValueError("data_root must contain frozen_validation_data.json")
        if not self.question.strip():
            raise ValueError("question is required")
        if not self.docker_image.strip() or _HASH.fullmatch(self.docker_image_id) is None:
            raise ValueError("an explicit local image and immutable sha256 image id are required")
        for name, lower, upper in (
            ("label_horizon_sessions", 1, 252),
            ("max_sessions", 8, 500),
            ("max_symbols", 2, 500),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{name} must be {lower}..{upper}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if self.specialist_id not in _SPECIALISTS:
            raise ValueError("specialist_id must name one registered research specialist")
        selected = self.specialist_ids or (self.specialist_id,)
        if tuple(sorted(selected)) != selected or len(set(selected)) != len(selected):
            raise ValueError("specialist_ids must be unique and stable-sorted")
        if not 1 <= len(selected) <= 6 or not set(selected).issubset(_SPECIALISTS):
            raise ValueError("specialist_ids must name one to six registered specialists")
        object.__setattr__(self, "specialist_ids", selected)
        if self.output_kind not in {
            "ScoreTable", "RegressionTable", "RegimeComparisonTable",
            "EventTable", "DiagnosticTable", "DatasetAssessmentTable",
        }:
            raise ValueError("unsupported canary output kind")
        output_kinds = self.specialist_output_kinds
        if output_kinds and tuple(sorted(output_kinds)) != output_kinds:
            raise ValueError("specialist_output_kinds must be stable-sorted")
        if output_kinds and {item[0] for item in output_kinds} != set(selected):
            raise ValueError("specialist_output_kinds must cover selected specialists")
        if output_kinds and len({item[0] for item in output_kinds}) != len(output_kinds):
            raise ValueError("specialist_output_kinds contains duplicates")
        allowed_outputs = {
            "ScoreTable", "RegressionTable", "RegimeComparisonTable",
            "EventTable", "DiagnosticTable", "DatasetAssessmentTable",
        }
        if any(item[1] not in allowed_outputs for item in output_kinds):
            raise ValueError("specialist_output_kinds contains an unsupported output kind")
        if tuple(sorted(self.vibe_tool_names)) != self.vibe_tool_names or len(set(self.vibe_tool_names)) != len(self.vibe_tool_names):
            raise ValueError("vibe_tool_names must be unique and stable-sorted")
        if not set(self.vibe_tool_names).issubset(VIBE_READONLY_RESEARCH_TOOLS):
            raise ValueError("vibe_tool_names contains an unapproved research tool")
        object.__setattr__(self, "data_root", data_root)
        object.__setattr__(self, "artifact_root", artifact_root)
        object.__setattr__(self, "dotenv_path", Path(self.dotenv_path).resolve())
        if (self.resume_source_path is None) != (self.resume_source_hash is None):
            raise ValueError("resume source path and hash must be supplied together")
        if self.resume_source_path is not None:
            source_path = Path(self.resume_source_path).resolve()
            if not source_path.is_file() or _HASH.fullmatch(str(self.resume_source_hash)) is None:
                raise ValueError("resume source must be an existing content-addressed real-agent draft")
            actual = "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
            if actual != self.resume_source_hash:
                raise ValueError("resume source hash mismatch")
            object.__setattr__(self, "resume_source_path", source_path)
        if self.resume_sources and self.resume_source_path is not None:
            raise ValueError("single-source and multi-source resume cannot be combined")
        resolved_sources = []
        seen_specialists = set()
        for item in self.resume_sources:
            if not isinstance(item, tuple) or len(item) != 3:
                raise ValueError("resume_sources must contain specialist/path/hash triples")
            specialist_id, raw_path, source_hash = item
            if specialist_id not in selected or specialist_id in seen_specialists:
                raise ValueError("resume source specialist is duplicate or not selected")
            source_path = Path(raw_path).resolve()
            if not source_path.is_file() or _HASH.fullmatch(str(source_hash)) is None:
                raise ValueError("resume source must be an existing content-addressed real-agent draft")
            actual = "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
            if actual != source_hash:
                raise ValueError("resume source hash mismatch")
            resolved_sources.append((specialist_id, source_path, source_hash))
            seen_specialists.add(specialist_id)
        if resolved_sources and seen_specialists != set(selected):
            raise ValueError("multi-specialist resume requires one source for every selected specialist")
        object.__setattr__(self, "resume_sources", tuple(sorted(resolved_sources)))

    def output_kind_for(self, specialist_id: str) -> str:
        if not self.specialist_output_kinds:
            if specialist_id not in _SPECIALISTS:
                raise ValueError("specialist is absent from canary output contracts")
            return self.output_kind
        try:
            return dict(self.specialist_output_kinds)[specialist_id]
        except KeyError as exc:
            raise ValueError("specialist is absent from canary output contracts") from exc

    @property
    def resolved_specialist_output_kinds(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (specialist_id, self.output_kind_for(specialist_id))
            for specialist_id in self.specialist_ids
        )


@dataclass(frozen=True, slots=True)
class PreparedCanaryData:
    feature_rows: tuple[FrozenFeatureObservation, ...]
    training_rows: tuple[DockerTrainingObservation, ...]
    labels: tuple[LabelRow, ...]
    training_cutoff: str
    availability_policy_hash: str
    visible_fields: tuple[str, ...]


class FrozenMetadataToolbox(ResearchToolboxProtocol):
    """Two exact, read-only metadata tools with no path or row-data surface."""

    _NAMES = ("describe_available_data", "query_data_schema")

    def __init__(self, *, task: ResearchAgentTask, fields: tuple[str, ...], row_count: int) -> None:
        if not set(self._NAMES).issubset(task.allowed_tools):
            raise ValueError("canary task must include the metadata-tool whitelist")
        self.task = task
        self.fields = fields
        self.row_count = row_count
        self._receipts: list[InvestigationReceipt] = []

    @property
    def names(self) -> tuple[str, ...]:
        return self._NAMES

    @property
    def definitions(self) -> tuple[Mapping[str, object], ...]:
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        return (
            {"name": "describe_available_data", "description": "Describe the approved frozen as-of panel.", "parameters": empty, "readonly": True},
            {"name": "query_data_schema", "description": "Read the label-free frozen feature schema.", "parameters": empty, "readonly": True},
        )

    @property
    def recorded_receipts(self) -> tuple[InvestigationReceipt, ...]:
        return tuple(sorted(self._receipts, key=lambda item: (item.tool_name, item.arguments_hash)))

    def call(self, name: str, arguments: Mapping[str, Any]) -> ResearchToolResult:
        if name not in self._NAMES or dict(arguments):
            raise PermissionError("undeclared tool or arguments")
        if name == "describe_available_data":
            payload = {
                "authority": "FROZEN_VISIBLE_INPUT",
                "data_snapshot_hash": self.task.data_snapshot_hash,
                "row_count": self.row_count,
                "time_semantics": "event_time < available_at < decision_time",
            }
        else:
            payload = {"authority": "FROZEN_VISIBLE_INPUT", "fields": list(self.fields)}
        result = ResearchToolResult(payload=MappingProxyType(payload), available_at=self.task.decision_time)
        self._receipts.append(
            InvestigationReceipt(
                tool_name=name,
                arguments_hash=canonical_hash({}),
                result_hash=canonical_hash(payload),
                available_at=result.available_at,
            )
        )
        return result


class CanaryExperimentBinding(ProductionExperimentBinding):
    """Require every prediction input to have one exactly clock-matched score."""

    def __init__(self, *, label_horizon_sessions: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.label_horizon_sessions = label_horizon_sessions

    def parse_table(self, **kwargs: Any):
        table = super().parse_table(**kwargs)
        if not isinstance(table, ScoreTable):
            return table
        request = kwargs["review_request"]
        spec = self.execution_specs[request.proposer_id]
        expected = {
            (row.symbol, row.decision_time, row.source_available_at)
            for row in spec.feature_rows
        }
        actual = {
            (row.symbol, row.decision_time, row.source_available_at)
            for row in table.rows
        }
        if actual != expected or len(table.rows) != len(spec.feature_rows):
            raise ValueError("score rows do not exactly match frozen prediction rows")
        if any(row.label_horizon_sessions != self.label_horizon_sessions for row in table.rows):
            raise ValueError("score label horizon differs from the canary contract")
        return table


RuntimeFactory = Callable[[ResearchAgentTask, ContentAddressedHandleStore], ResearchAgentRuntimeProtocol]
RunnerFactory = Callable[[ProductCanaryConfiguration], Any]


class AgentResearchProductCanary:
    """Assemble and execute the unique product path from one frozen repository."""

    def __init__(self, configuration: ProductCanaryConfiguration) -> None:
        self.configuration = configuration

    def run(
        self,
        *,
        runtime_factory: RuntimeFactory | None = None,
        runner_factory: RunnerFactory | None = None,
    ) -> ProductRunResult:
        config = self.configuration
        frozen = FrozenValidationDataRepository(config.data_root).load(require_strict_provenance=True)
        if frozen is None:
            raise ValueError("frozen validation data is unavailable")
        if frozen.source_label != _SOURCE_LABEL:
            raise ValueError("product canary requires the approved proxy frozen source")
        prepared = _prepare_data(
            frozen,
            horizon=config.label_horizon_sessions,
            max_sessions=config.max_sessions,
            max_symbols=config.max_symbols,
        )
        registered_factors = _load_registered_factors(config.data_root, frozen)
        if config.vibe_tool_names:
            _probe_registry, active_vibe_tools, unavailable_vibe_tools = available_vibe_research_registry(
                config.vibe_tool_names
            )
            del _probe_registry
        else:
            active_vibe_tools, unavailable_vibe_tools = (), ()
        policy = ExperimentReviewPolicy()
        amendment = _load_amendment(policy)
        run_id = "agent_product_canary_" + canonical_hash(
            {
                "question": config.question,
                "snapshot": frozen.snapshot.artifact_hash,
                "image": config.docker_image_id,
                "horizon": config.label_horizon_sessions,
                "seed": config.seed,
                "vibe_tools": config.vibe_tool_names,
                "vibe_network": config.allow_vibe_network_tools,
                "specialists": config.specialist_ids,
                "specialist_output_kinds": config.resolved_specialist_output_kinds,
            }
        )[7:23]
        manifest = ResearchRunManifest(
            run_id=run_id,
            campaign_hash=canonical_hash({"question": config.question, "mode": "REAL_AGENT_PRODUCT_CANARY"}),
            intent_hash=canonical_hash({"question": config.question}),
            data_plan_hash=canonical_hash({
                "snapshot": frozen.snapshot.artifact_hash,
                "source": frozen.source_label,
                "dynamic_research_tools": config.vibe_tool_names,
            }),
            route_plan_hash=canonical_hash({
                "specialists": config.specialist_ids,
                "specialist_output_kinds": config.resolved_specialist_output_kinds,
                "research_tool_profile": active_vibe_tools,
                "unavailable_optional_tools": unavailable_vibe_tools,
            }),
            policy_hash=policy.artifact_hash,
            code_hash=canonical_hash({"component": "agent_research_product.canary", "version": _SCHEMA_VERSION}),
            selected_group_ids=config.specialist_ids,
            candidate_request_hashes=(),
            created_at=frozen.snapshot.decision_time,
            runtime_version="agent_research_product_canary_v1",
            provider_id="configured_research_provider",
            model_id="configured_model",
            data_snapshot_hash=frozen.snapshot.artifact_hash,
            current_stage=ResearchRunLifecycle.PLANNING,
            remaining_iterations=12 * len(config.specialist_ids),
            remaining_tool_calls=max(8, len(active_vibe_tools) + 2) * len(config.specialist_ids),
            remaining_candidate_slots=4 * len(config.specialist_ids),
        )
        config.artifact_root.mkdir(parents=True, exist_ok=True)
        draft_store = ContentAddressedHandleStore(config.artifact_root / "draft_sources")
        tasks = []
        runtimes = {}
        toolboxes = {}
        resume_sources = {
            specialist_id: (source_path, source_hash)
            for specialist_id, source_path, source_hash in config.resume_sources
        }
        for specialist_id in config.specialist_ids:
            output_kind = config.output_kind_for(specialist_id)
            task = ResearchAgentTask(
                task_id=f"task_{specialist_id}",
                run_id=run_id,
                specialist_id=specialist_id,
                question=_task_question(
                    config,
                    prepared.visible_fields,
                    specialist_id=specialist_id,
                    unavailable_tools=unavailable_vibe_tools,
                ),
                campaign_hash=manifest.campaign_hash,
                data_snapshot_hash=frozen.snapshot.artifact_hash,
                decision_time=frozen.snapshot.decision_time,
                allowed_tools=tuple(sorted(set(FrozenMetadataToolbox._NAMES) | set(active_vibe_tools))),
                output_kind=output_kind,
                max_iterations=12,
                timeout_seconds=600.0,
            )
            metadata_toolbox = FrozenMetadataToolbox(
                task=task,
                fields=prepared.visible_fields,
                row_count=len(prepared.feature_rows),
            )
            metadata_toolbox.call("describe_available_data", {})
            metadata_toolbox.call("query_data_schema", {})
            if active_vibe_tools:
                vibe_toolbox = VibeResearchToolbox(
                    task=task,
                    tool_names=active_vibe_tools,
                    handle_store=ContentAddressedHandleStore(
                        config.artifact_root / "research_tool_outputs" / specialist_id
                    ),
                    allow_network=config.allow_vibe_network_tools,
                )
                toolbox: ResearchToolboxProtocol = CompositeResearchToolbox(metadata_toolbox, vibe_toolbox)
            else:
                toolbox = metadata_toolbox
            runtime = (
                runtime_factory(task, draft_store)
                if runtime_factory is not None
                else (
                    _PersistedDraftWithRevisionRuntime(
                        persisted=_PersistedRealDraftRuntime(
                            source_path=resume_sources[specialist_id][0],
                            expected_hash=resume_sources[specialist_id][1],
                            draft_store=draft_store,
                        ),
                        revision_runtime=_real_runtime(config, draft_store),
                    )
                    if specialist_id in resume_sources
                    else (
                        _PersistedRealDraftRuntime(
                            source_path=config.resume_source_path,
                            expected_hash=str(config.resume_source_hash),
                            draft_store=draft_store,
                        )
                        if config.resume_source_path is not None and len(config.specialist_ids) == 1
                        else _real_runtime(config, draft_store)
                    )
                )
            )
            tasks.append(task)
            runtimes[specialist_id] = runtime
            toolboxes[task.task_id] = toolbox
        authority_manifest = load_authority_manifest(
            config.data_root,
            snapshot_hash=frozen.snapshot.artifact_hash,
        )
        evidence_context = ProductionEvidenceContext(
            snapshot=frozen.snapshot,
            labels=prepared.labels,
            dataset_hash=frozen.artifact_hash,
            availability_policy_hash=prepared.availability_policy_hash,
            split_hash=canonical_hash(
                {
                    "training_cutoff": prepared.training_cutoff,
                    "horizon": config.label_horizon_sessions,
                    "prediction_rows": tuple(row.to_runner_row() for row in prepared.feature_rows),
                }
            ),
            cost_model=CostModel(spread_bps=2.0, linear_impact_bps=1.0),
            registered_factors=registered_factors,
            seed=config.seed,
            authority_ceiling=(
                None if authority_manifest is None else str(authority_manifest["authority_ceiling"])
            ),
            authority_reason=(
                None if authority_manifest is None else str(authority_manifest["authority_reason"])
            ),
        )
        binding = CanaryExperimentBinding(
            label_horizon_sessions=config.label_horizon_sessions,
            amendment=amendment,
            policy=policy,
            python_runtime_hash=config.docker_image_id,
            dependency_manifest_hash=canonical_hash(
                {"image_id": config.docker_image_id, "entrypoint": "TRAIN_PREDICT_V2"}
            ),
            evidence_context=evidence_context,
            execution_specs={
                specialist_id: ExperimentExecutionSpec(
                    output_kind=config.output_kind_for(specialist_id),
                    feature_rows=prepared.feature_rows,
                    training_rows=prepared.training_rows,
                    training_cutoff=prepared.training_cutoff,
                )
                for specialist_id in config.specialist_ids
            },
        )
        service = AgentResearchProductService(
            root=config.artifact_root,
            scheduler=ResearchSwarmSchedulerAdapter(
                checkpoint_store=BranchCheckpointStore(config.artifact_root),
                max_workers=len(config.specialist_ids),
            ),
            source_repository=ContentAddressedDraftSourceRepository(draft_store),
            review_gate=ExperimentReviewGate(),
            runner=(
                runner_factory(config)
                if runner_factory is not None
                else DockerResearchRunner(
                    DockerResearchConfig(
                        sandbox_root=config.artifact_root / "docker",
                        image_name=config.docker_image,
                        expected_image_id=config.docker_image_id,
                        timeout_seconds=120.0,
                    )
                )
            ),
            binding=binding,
            evidence_evaluator=UnifiedResearchEvidenceEvaluator(),
        )
        return service.run(
            ProductRunRequest(
                manifest=manifest,
                question_id="question_" + canonical_hash({"question": config.question})[7:19],
                question=config.question,
                tasks=tuple(sorted(tasks, key=lambda item: item.task_id)),
                runtimes=runtimes,
                toolboxes=toolboxes,
            )
        )


def _real_runtime(config: ProductCanaryConfiguration, draft_store: ContentAddressedHandleStore):
    provider = ConfiguredResearchLLMFactory(
        dotenv_path=config.dotenv_path,
        minimum_max_tokens=8_192,
    )
    return VibeAgentLoopAdapter(
        loop_factory=SafeVibeAgentLoopFactory(
            llm_factory=provider,
            run_root=config.artifact_root / "agent_runs",
        ),
        draft_store=draft_store,
    )


class _PersistedRealDraftRuntime:
    """Resume a content-addressed draft produced by a prior real-provider attempt."""

    def __init__(self, *, source_path: Path, expected_hash: str, draft_store: ContentAddressedHandleStore) -> None:
        self.source_path = source_path
        self.expected_hash = expected_hash
        self.draft_store = draft_store

    def run(self, *, task, toolbox, upstream_context, cancellation):
        del upstream_context
        if cancellation.cancelled:
            raise RuntimeError("resume was cancelled")
        source = self.source_path.read_bytes()
        handle = self.draft_store.put_bytes(source, media_type="text/x-python-draft")
        if handle.handle_id != self.expected_hash:
            raise ValueError("resumed real-agent source identity changed")
        plan_text = "恢复并复核此前真实模型生成的研究程序；保留原源码身份，只针对机械审查结果继续执行。"
        plan_hash = self.draft_store.put_json({"plan": plan_text}).handle_id
        drafts = tuple(sorted((
            ResearchDraftArtifact(kind=ResearchDraftKind.CODE, content_hash=handle.handle_id, media_type="text/x-python-draft"),
            ResearchDraftArtifact(kind=ResearchDraftKind.PLAN, content_hash=plan_hash, media_type="application/json"),
        ), key=lambda item: (item.kind.value, item.content_hash)))
        return ResearchAgentOutcome(
            task_id=task.task_id,
            task_hash=task.artifact_hash,
            specialist_id=task.specialist_id,
            status=ResearchAgentStatus.COMPLETED,
            plan_hash=plan_hash,
            investigation_receipts=toolbox.recorded_receipts,
            draft_artifacts=drafts,
            provider_calls=1,
            tool_calls=len(toolbox.recorded_receipts),
        )


class _PersistedDraftWithRevisionRuntime:
    """Reuse a committed draft, calling the real AgentLoop only for one repair."""

    supports_review_revision = True

    def __init__(self, *, persisted: _PersistedRealDraftRuntime, revision_runtime: Any) -> None:
        self.persisted = persisted
        self.revision_runtime = revision_runtime

    def run(self, *, task, toolbox, upstream_context, cancellation):
        if upstream_context:
            # A runner/review repair is deliberately smaller than an open-ended
            # research pass. Two iterations are enough to read typed feedback,
            # patch the existing source and return one DRAFT without making a
            # minor code defect look like a frozen process.
            repair_task = replace(
                task,
                max_iterations=min(2, task.max_iterations),
                timeout_seconds=min(180.0, task.timeout_seconds),
            )
            return self.revision_runtime.run(
                task=repair_task,
                toolbox=toolbox,
                upstream_context=upstream_context,
                cancellation=cancellation,
            )
        return self.persisted.run(
            task=task,
            toolbox=toolbox,
            upstream_context=upstream_context,
            cancellation=cancellation,
        )


def _prepare_data(frozen: Any, *, horizon: int, max_sessions: int, max_symbols: int) -> PreparedCanaryData:
    rows = tuple(sorted(frozen.snapshot.rows, key=lambda row: (row.event_time, row.symbol)))
    visible_fields = tuple(
        field for field in frozen.snapshot.fields
        if float(frozen.snapshot.coverage_by_field.get(field, 0.0)) > 0.0
    )
    visible_field_set = set(visible_fields)
    if not {"close", "volume"}.issubset(visible_field_set):
        raise ValueError("frozen repository must expose non-empty close and volume fields")
    session_times = tuple(sorted({row.event_time for row in rows}))
    if len(session_times) < max(8, horizon + 4):
        raise ValueError("frozen repository has too few sessions for separated training and prediction")
    index = {when.date(): position for position, when in enumerate(session_times)}
    labels = dict(frozen.labels)
    provenance = {(item.event_time, item.symbol): item for item in frozen.row_provenance}
    eligible = []
    for row in rows:
        key = f"{row.event_time.isoformat()}|{row.symbol}"
        item = provenance.get((row.event_time, row.symbol))
        if key not in labels or item is None:
            continue
        # The approved market spine has three distinct clocks.  A repository
        # with weaker clocks is not silently upgraded by this canary.
        if item.available_at != row.event_time + timedelta(hours=1):
            continue
        if not isinstance(row.values.get("close"), (int, float)) or not isinstance(row.values.get("volume"), (int, float)):
            continue
        if float(row.values["volume"]) <= 0:
            continue
        eligible.append(row)
    eligible_sessions = tuple(sorted({row.event_time for row in eligible}))[-max_sessions:]
    if len(eligible_sessions) < 8:
        raise ValueError("frozen repository has too few labelled PIT sessions")
    prediction_count = max(2, min(10, len(eligible_sessions) // 4))
    prediction_sessions = set(eligible_sessions[-prediction_count:])
    symbols = tuple(sorted({row.symbol for row in eligible if row.event_time in prediction_sessions}))[:max_symbols]
    prediction = tuple(
        row for row in eligible if row.event_time in prediction_sessions and row.symbol in symbols
    )
    if len(prediction) < 4 or len({row.symbol for row in prediction}) < 2:
        raise ValueError("frozen repository cannot form a multi-row prediction panel")
    first_prediction = min(row.event_time for row in prediction) + timedelta(hours=2)
    training = []
    for row in eligible:
        if row.event_time not in set(eligible_sessions[:-prediction_count]) or row.symbol not in symbols:
            continue
        position = index[row.event_time.date()]
        if position + horizon >= len(session_times):
            continue
        outcome_available = session_times[position + horizon] + timedelta(hours=2)
        if outcome_available > first_prediction:
            continue
        training.append(
            DockerTrainingObservation(
                symbol=row.symbol,
                decision_time=(row.event_time + timedelta(hours=2)).isoformat(),
                source_available_at=(row.event_time + timedelta(hours=1)).isoformat(),
                outcome_available_at=outcome_available.isoformat(),
                target_value=float(labels[f"{row.event_time.isoformat()}|{row.symbol}"]),
                features={field: _clean_feature_value(row.values.get(field)) for field in visible_fields},
            )
        )
    if len(training) < 4:
        raise ValueError("frozen repository cannot form leakage-separated training rows")
    observations = tuple(
        FrozenFeatureObservation(
            symbol=row.symbol,
            decision_time=row.event_time + timedelta(hours=2),
            source_available_at=row.event_time + timedelta(hours=1),
            features={field: _clean_feature_value(row.values.get(field)) for field in visible_fields},
        )
        for row in prediction
    )
    label_rows = tuple(
        LabelRow(
            event_time=row.event_time + timedelta(hours=2),
            symbol=row.symbol,
            value=float(labels[f"{row.event_time.isoformat()}|{row.symbol}"]),
            # The repository persists feature sessions and label values but
            # not the future label session itself.  Keep prediction labels
            # physically outside the research window and use a conservative
            # after-horizon clock instead of inventing an in-window timestamp.
            available_at=(row.event_time + timedelta(hours=2, days=horizon * 2)),
        )
        for row in prediction
    )
    policy_hashes = {item.availability_policy_hash for item in frozen.row_provenance}
    if len(policy_hashes) != 1:
        raise ValueError("frozen repository has ambiguous availability policy identities")
    return PreparedCanaryData(
        feature_rows=observations,
        training_rows=tuple(training),
        labels=tuple(sorted(label_rows, key=lambda row: (row.event_time, row.symbol))),
        training_cutoff=first_prediction.isoformat(),
        availability_policy_hash=next(iter(policy_hashes)),
        visible_fields=visible_fields,
    )


def _clean_feature_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("frozen feature values must be scalar JSON values")


def _load_registered_factors(data_root: Path, frozen: Any) -> tuple[RegisteredFactor, ...]:
    receipt_path = data_root / "registered_baselines_receipt.json"
    if not receipt_path.is_file():
        return ()
    raw = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("registered baseline receipt is invalid")
    claimed = raw.pop("artifact_hash", None)
    if claimed != canonical_hash(raw) or raw.get("status") != "PASSED":
        raise ValueError("registered baseline receipt hash mismatch")
    if raw.get("snapshot_hash") != frozen.snapshot.artifact_hash:
        raise ValueError("registered baseline receipt belongs to another snapshot")
    factors = []
    for item in raw.get("factors", ()):
        field = str(item["field_name"])
        if field not in frozen.snapshot.fields:
            raise ValueError("registered baseline field is absent from the snapshot")
        scores = {
            f"{(row.event_time + timedelta(hours=2)).isoformat()}|{row.symbol}": float(row.values[field])
            for row in frozen.snapshot.rows
            if isinstance(row.values.get(field), (int, float))
            and not isinstance(row.values.get(field), bool)
            and math.isfinite(float(row.values[field]))
        }
        if len(scores) != int(item["score_count"]):
            raise ValueError("registered baseline score count changed")
        factors.append(RegisteredFactor(
            factor_id=str(item["factor_id"]),
            factor_hash=str(item["factor_hash"]),
            scores=scores,
        ))
    return tuple(sorted(factors, key=lambda item: item.factor_id))


def _load_amendment(policy: ExperimentReviewPolicy) -> SandboxAmendmentIdentity:
    receipt_path = Path(".autobuild/veriquant/research500_harness/constitution_amendment_hash.json").resolve()
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    constitution_hash = str(payload.get("sha256") or "")
    if _HASH.fullmatch(constitution_hash) is None or payload.get("amendment_version") != AMENDMENT_VERSION:
        raise ValueError("active research-sandbox amendment receipt is unavailable")
    return SandboxAmendmentIdentity(
        amendment_id="A-02",
        amendment_version=AMENDMENT_VERSION,
        constitution_hash=constitution_hash,
        policy_hash=policy.artifact_hash,
        authorized_at=_AMENDMENT_AUTHORIZED_AT,
    )


def _task_question(
    config: ProductCanaryConfiguration,
    fields: tuple[str, ...],
    *,
    specialist_id: str | None = None,
    unavailable_tools: tuple[str, ...] = (),
) -> str:
    output_kind = config.output_kind_for(specialist_id or config.specialist_id)
    output_rule = (
        f"必须为每个 prediction row 输出一个 ScoreRow，label_horizon_sessions={config.label_horizon_sessions}。"
        "原样复制 symbol、decision_time、source_available_at；不得读取标签。"
        if output_kind == "ScoreTable"
        else f"必须输出 {output_kind} 合同要求的 rows；不得伪造表或行身份字段。"
    )
    dynamic_rule = (
        "Vibe 动态工具结果只用于调查；只有列在可见字段中的值才会进入 Docker 数据身份。"
        "如果调查概念尚未成为可见字段，必须在 proxy_specs 或 data_gaps 中披露，"
        "可以用明确代理完成辅助实验，但不得声称直接验证了新闻、研报或其他未冻结概念。"
        if config.vibe_tool_names else ""
    )
    return (
        f"{config.question}\n"
        f"你的专业研究组是 {specialist_id or config.specialist_id}。先从本组角度规划多个可证伪实验。"
        "提交一个 DRAFT 研究程序，使用 TRAIN_PREDICT_V2 三参数入口。"
        f"{output_rule}"
        "输入协议：training_rows 的研究字段位于 item['features']['字段名']，"
        "prediction_rows 的研究字段直接位于 item['字段名']；"
        "必须定义并统一使用一个特征访问函数：若 'features' in item 则读取 "
        "item['features'][field]，否则读取 item[field]；禁止在 lambda 或特殊分支中直接读取研究字段。"
        "训练标签仅可用于拟合，不得从 prediction_rows 获取标签。"
        "访问输入字段只使用 item['field']，不要调用名为 get 的方法。"
        f"可见字段：{','.join(fields)}。"
        f"{dynamic_rule}"
        f"本机未安装的可选工具：{','.join(unavailable_tools) or '无'}；如确实需要，记录 data_gaps，不得伪造调用结果。"
    )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent)
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the real AlphaForge Agent research product canary")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--docker-image", required=True)
    parser.add_argument("--docker-image-id", required=True)
    parser.add_argument("--dotenv", type=Path, default=Path("agent/.env"))
    parser.add_argument("--label-horizon", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-sessions", type=int, default=80)
    parser.add_argument("--max-symbols", type=int, default=20)
    parser.add_argument(
        "--output-kind",
        default="ScoreTable",
        choices=(
            "ScoreTable", "RegressionTable", "RegimeComparisonTable",
            "EventTable", "DiagnosticTable", "DatasetAssessmentTable",
        ),
    )
    parser.add_argument("--specialist", default="market_structure", choices=tuple(sorted(_SPECIALISTS)))
    parser.add_argument("--resume-source", type=Path)
    parser.add_argument("--resume-source-hash")
    parser.add_argument(
        "--vibe-tool",
        action="append",
        default=[],
        choices=VIBE_READONLY_RESEARCH_TOOLS,
        help="Enable one approved Vibe read-only research tool (repeatable).",
    )
    parser.add_argument(
        "--allow-vibe-network-tools",
        action="store_true",
        help="Permit explicitly selected Vibe tools to contact their configured read-only data providers.",
    )
    args = parser.parse_args(argv)
    output_root = args.artifact_root.resolve()
    try:
        result = AgentResearchProductCanary(
            ProductCanaryConfiguration(
                data_root=args.data_root,
                artifact_root=output_root,
                question=args.question,
                docker_image=args.docker_image,
                docker_image_id=args.docker_image_id,
                label_horizon_sessions=args.label_horizon,
                seed=args.seed,
                max_sessions=args.max_sessions,
                max_symbols=args.max_symbols,
                output_kind=args.output_kind,
                specialist_id=args.specialist,
                dotenv_path=args.dotenv,
                resume_source_path=args.resume_source,
                resume_source_hash=args.resume_source_hash,
                vibe_tool_names=tuple(sorted(set(args.vibe_tool))),
                allow_vibe_network_tools=args.allow_vibe_network_tools,
            )
        ).run()
        payload = {"schema_version": _SCHEMA_VERSION, **result.to_dict()}
        expected_status = "OFFICIAL_EVIDENCE"
        passed = result.lifecycle in {"COMPLETED", "PARTIAL"} and any(
            item.status.value == expected_status for item in result.experiments
        )
        payload["canary_passed"] = passed
        payload["question_hash"] = canonical_hash({"question": args.question})
        payload["output_kind"] = args.output_kind
        if args.resume_source_hash:
            payload["recovered_from_real_source_hash"] = args.resume_source_hash
    except Exception:
        # Provider, Docker and repository exception strings may contain local
        # transport or path details; the public CLI emits only a typed shell.
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "canary_passed": False,
            "status": "FAILED",
            "typed_failure": "PRODUCT_CANARY_FAILED",
        }
    _atomic_json(output_root / "canary_result.json", payload)
    receipt = _acceptance_receipt(payload, question=args.question, result_path=output_root / "canary_result.json")
    _atomic_json(output_root / "acceptance_receipt.json", receipt)
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0 if payload["canary_passed"] else 1


def _acceptance_receipt(payload: Mapping[str, Any], *, question: str, result_path: Path) -> dict[str, Any]:
    experiments = list(payload.get("experiments") or ())
    passed = payload.get("canary_passed") is True and len(experiments) == 1
    experiment = experiments[0] if experiments else {}
    value = {
        "schema_version": "alphaforge.first-real-e2e-canary-receipt/v1",
        "status": "PASSED" if passed else "BLOCKED",
        "canary_passed": passed,
        "run_id": payload.get("run_id"),
        "question_hash": canonical_hash({"question": question}),
        "output_kind": payload.get("output_kind"),
        "evidence_authority": experiment.get("status"),
        "failure_code": None if passed else (experiment.get("failure_code") or "PRODUCT_CANARY_FAILED"),
        "recovery_status": (
            "RESUMED_FROM_CONTENT_ADDRESSED_REAL_AGENT_DRAFT"
            if payload.get("recovered_from_real_source_hash")
            else "FRESH_REAL_PROVIDER_RUN"
        ),
        "source_hash": experiment.get("source_hash"),
        "review_hash": experiment.get("review_hash"),
        "runner_receipt_hash": experiment.get("runner_receipt_hash"),
        "evidence_result_hash": experiment.get("evidence_result_hash"),
        "official_evidence_hash": experiment.get("official_evidence_hash"),
        "report_hash": payload.get("report_hash"),
        "report_path": payload.get("report_path"),
        "canary_result_path": str(result_path.resolve()),
    }
    value["artifact_hash"] = canonical_hash(value)
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AgentResearchProductCanary",
    "CanaryExperimentBinding",
    "FrozenMetadataToolbox",
    "PreparedCanaryData",
    "ProductCanaryConfiguration",
    "main",
]
