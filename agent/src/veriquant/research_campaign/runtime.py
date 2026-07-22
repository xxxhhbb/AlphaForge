"""Campaign-routed multi-Desk runtime for Real-LLM research.

This runtime is a product-layer scheduler.  It runs only the desks selected by
the Campaign Router, follows the method-specific protocol plan stage-by-stage,
and stores hash-only stage records in stable task order.  LLM-facing stages may
emit only DRAFT or explanation artifacts.  Evidence, Promotion, Portfolio,
Risk, Execution, and Ledger authority remain outside this package.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, Protocol

from ..contracts import CanonicalContract, Component, ContractValidationError, FailureCode
from ..contracts.base import (
    canonicalize,
    fail,
    freeze_mapping,
    require_aware_datetime,
    require_hash,
    require_identifier,
    require_text,
)
from ..llm_desks.api import (
    CompilePITArtifact,
    DeskId,
    DeskLLMConfig,
    DeskRunResult,
    LLMDeskStatus,
    ProviderBudget,
    ProviderTrace,
    ResearchQuestionView,
    ResearchStageController,
)
from ..llm_desks.safety import assert_no_forbidden_content, canonical_hash
from .models import CampaignRoute, CampaignState, CampaignTaskPlan, ResearchCampaign, require_campaign_executable
from .protocols import CampaignProtocolPlan, CampaignTaskProtocolPlan, ResearchProtocolKind


CAMPAIGN_RUNTIME_RULE_VERSION = "veriquant.campaign-runtime/v1"
DEFAULT_RUNTIME_STARTED_AT = datetime(2026, 7, 19, 11, 30, tzinfo=timezone.utc)
MAX_CAMPAIGN_DESKS = 6


class CampaignRuntimeStatus(str, Enum):
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    AWAITING_CHECKPOINT = "AWAITING_CHECKPOINT"


class BranchRuntimeStatus(str, Enum):
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    SKIPPED_UPSTREAM = "SKIPPED_UPSTREAM"


class StageRuntimeStatus(str, Enum):
    DRAFT = "DRAFT"
    EXPLANATION = "EXPLANATION"
    CONTROL_REFERENCE = "CONTROL_REFERENCE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


_LLM_STAGE_OUTPUTS = frozenset({StageRuntimeStatus.DRAFT, StageRuntimeStatus.EXPLANATION})
_FINAL_EXPLANATION_STAGES = (
    "feedback",
    "conclusion",
    "diagnosis",
    "assessment",
    "report",
    "recommend",
    "human_decision",
)
_CONTROL_STAGES = (
    "pit",
    "evaluate",
    "evidence",
    "retrieve_registered_evidence",
    "attribution",
    "regime",
    "adversarial",
    "coverage",
    "reproduce",
    "detect_drift",
)


@dataclass(frozen=True, kw_only=True)
class CampaignRuntimeConfig(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUNTIME_RULE_VERSION

    max_parallel_desks: int = MAX_CAMPAIGN_DESKS
    stable_sort_results: bool = True
    fail_closed_on_artifact_collision: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.max_parallel_desks, bool) or not isinstance(self.max_parallel_desks, int):
            fail(FailureCode.INVALID_VALUE, "max_parallel_desks must be an integer")
        if not 1 <= self.max_parallel_desks <= MAX_CAMPAIGN_DESKS:
            fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "runtime may run at most three desks")
        for name in ("stable_sort_results", "fail_closed_on_artifact_collision"):
            if not isinstance(getattr(self, name), bool):
                fail(FailureCode.INVALID_VALUE, f"{name} must be boolean")


@dataclass(frozen=True, kw_only=True)
class CampaignRuntimeControl(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUNTIME_RULE_VERSION

    cancel_requested: bool = False
    stop_at_next_checkpoint: bool = False
    started_at: datetime = DEFAULT_RUNTIME_STARTED_AT

    def __post_init__(self) -> None:
        if not isinstance(self.cancel_requested, bool) or not isinstance(self.stop_at_next_checkpoint, bool):
            fail(FailureCode.INVALID_VALUE, "runtime control flags must be boolean")
        require_aware_datetime(self.started_at, "started_at")


@dataclass(frozen=True, kw_only=True)
class CampaignStageExecutionContext:
    """Non-persisted in-process context passed to a stage executor."""

    campaign: ResearchCampaign
    route: CampaignRoute
    route_task: CampaignTaskPlan
    task_protocol: CampaignTaskProtocolPlan
    stage_id: str
    stage_index: int
    artifact_namespace: str


@dataclass(frozen=True, kw_only=True)
class CampaignStageResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUNTIME_RULE_VERSION

    route_task_id: str
    desk_id: str
    stage_id: str
    stage_index: int
    status: StageRuntimeStatus
    produced_by: Component
    output_hash: str | None
    prompt_hash: str | None
    trace_hash: str | None
    artifact_namespace: str
    failure_code: FailureCode | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.route_task_id, "route_task_id")
        require_identifier(self.desk_id, "desk_id")
        require_identifier(self.stage_id, "stage_id")
        if isinstance(self.stage_index, bool) or not isinstance(self.stage_index, int) or self.stage_index < 0:
            fail(FailureCode.INVALID_VALUE, "stage_index must be a non-negative integer")
        if not isinstance(self.status, StageRuntimeStatus):
            object.__setattr__(self, "status", StageRuntimeStatus(self.status))
        if not isinstance(self.produced_by, Component):
            object.__setattr__(self, "produced_by", Component(self.produced_by))
        for name in ("output_hash", "prompt_hash", "trace_hash"):
            value = getattr(self, name)
            if value is not None:
                require_hash(value, name)
        require_identifier(self.artifact_namespace, "artifact_namespace")
        if self.failure_code is not None and not isinstance(self.failure_code, FailureCode):
            object.__setattr__(self, "failure_code", FailureCode(self.failure_code))
        if self.message is not None:
            require_text(self.message, "message")
            assert_no_forbidden_content(self.message, where="campaign_runtime_stage.message")
        if self.produced_by in {Component.LLM_RESEARCHER, Component.RESEARCH_DESK} and self.status not in _LLM_STAGE_OUTPUTS:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "LLM/Research Desk stages may emit only DRAFT or explanation")
        if self.status in _LLM_STAGE_OUTPUTS and self.output_hash is None:
            fail(FailureCode.INVALID_HASH, "successful LLM stage requires an output hash")
        if self.status in {StageRuntimeStatus.BLOCKED, StageRuntimeStatus.CANCELLED, StageRuntimeStatus.SKIPPED}:
            if self.failure_code is None:
                fail(FailureCode.INVALID_VALUE, "non-successful stage requires a failure_code")
        if self.status is StageRuntimeStatus.CONTROL_REFERENCE and self.produced_by in {
            Component.LLM_RESEARCHER,
            Component.RESEARCH_DESK,
        }:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "LLM cannot produce control references")


@dataclass(frozen=True, kw_only=True)
class CampaignBranchResult(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUNTIME_RULE_VERSION

    route_task_id: str
    desk_id: str
    artifact_namespace: str
    status: BranchRuntimeStatus
    stage_results: tuple[CampaignStageResult, ...]
    failure_code: FailureCode | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.route_task_id, "route_task_id")
        require_identifier(self.desk_id, "desk_id")
        require_identifier(self.artifact_namespace, "artifact_namespace")
        if not isinstance(self.status, BranchRuntimeStatus):
            object.__setattr__(self, "status", BranchRuntimeStatus(self.status))
        if not isinstance(self.stage_results, tuple):
            fail(FailureCode.INVALID_VALUE, "stage_results must be a tuple")
        if not all(isinstance(item, CampaignStageResult) for item in self.stage_results):
            fail(FailureCode.INVALID_VALUE, "stage_results must contain CampaignStageResult")
        if tuple(item.stage_index for item in self.stage_results) != tuple(sorted(item.stage_index for item in self.stage_results)):
            fail(FailureCode.INVALID_STATE, "branch stage_results must be serially ordered")
        for item in self.stage_results:
            if item.route_task_id != self.route_task_id or item.desk_id != self.desk_id:
                fail(FailureCode.INVALID_VALUE, "stage result belongs to another branch")
        if self.failure_code is not None and not isinstance(self.failure_code, FailureCode):
            object.__setattr__(self, "failure_code", FailureCode(self.failure_code))
        if self.status is BranchRuntimeStatus.COMPLETED:
            if self.failure_code is not None or any(
                item.status in {StageRuntimeStatus.BLOCKED, StageRuntimeStatus.CANCELLED, StageRuntimeStatus.SKIPPED}
                for item in self.stage_results
            ):
                fail(FailureCode.INVALID_STATE, "completed branch cannot include failed stages")
        elif self.failure_code is None:
            fail(FailureCode.INVALID_VALUE, "non-completed branch requires failure_code")
        if self.message is not None:
            require_text(self.message, "message")
            assert_no_forbidden_content(self.message, where="campaign_runtime_branch.message")


@dataclass(frozen=True, kw_only=True)
class CampaignCommittedStage(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUNTIME_RULE_VERSION

    stage_key: str
    result: CampaignStageResult

    def __post_init__(self) -> None:
        require_identifier(self.stage_key, "stage_key")
        if not isinstance(self.result, CampaignStageResult):
            fail(FailureCode.INVALID_VALUE, "committed stage must contain CampaignStageResult")
        expected = stage_key_for(self.result.route_task_id, self.result.stage_id)
        if self.stage_key != expected:
            fail(FailureCode.INVALID_VALUE, "stage_key does not match result")


@dataclass(frozen=True, kw_only=True)
class CampaignRuntimeRecoveryState(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUNTIME_RULE_VERSION

    committed_stages: tuple[CampaignCommittedStage, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.committed_stages, tuple):
            fail(FailureCode.INVALID_VALUE, "committed_stages must be a tuple")
        if not all(isinstance(item, CampaignCommittedStage) for item in self.committed_stages):
            fail(FailureCode.INVALID_VALUE, "committed_stages must contain CampaignCommittedStage")
        keys = tuple(item.stage_key for item in self.committed_stages)
        if len(set(keys)) != len(keys):
            fail(FailureCode.INVALID_STATE, "duplicate committed stage keys")

    def by_key(self) -> Mapping[str, CampaignStageResult]:
        return MappingProxyType({item.stage_key: item.result for item in self.committed_stages})


@dataclass(frozen=True, kw_only=True)
class CampaignRuntimeManifest(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = CAMPAIGN_RUNTIME_RULE_VERSION

    run_id: str
    campaign_hash: str
    route_hash: str
    protocol_plan_hash: str
    runtime_version: str
    status: CampaignRuntimeStatus
    branch_results: tuple[CampaignBranchResult, ...]
    artifact_hashes: Mapping[str, str] = field(default_factory=dict)
    awaiting_checkpoint_id: str | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_hash(self.campaign_hash, "campaign_hash")
        require_hash(self.route_hash, "route_hash")
        require_hash(self.protocol_plan_hash, "protocol_plan_hash")
        require_identifier(self.runtime_version, "runtime_version")
        if not isinstance(self.status, CampaignRuntimeStatus):
            object.__setattr__(self, "status", CampaignRuntimeStatus(self.status))
        if not isinstance(self.branch_results, tuple):
            fail(FailureCode.INVALID_VALUE, "branch_results must be a tuple")
        if not all(isinstance(item, CampaignBranchResult) for item in self.branch_results):
            fail(FailureCode.INVALID_VALUE, "branch_results must contain CampaignBranchResult")
        task_ids = tuple(item.route_task_id for item in self.branch_results)
        if task_ids != tuple(sorted(task_ids)):
            fail(FailureCode.INVALID_STATE, "branch_results must be stable-sorted by route_task_id")
        if len(set(task_ids)) != len(task_ids):
            fail(FailureCode.INVALID_VALUE, "duplicate branch result")
        hashes = dict(self.artifact_hashes)
        for key, value in hashes.items():
            require_identifier(key, "artifact_hashes.key")
            require_hash(value, f"artifact_hashes.{key}")
        object.__setattr__(self, "artifact_hashes", MappingProxyType(dict(sorted(hashes.items()))))
        if self.awaiting_checkpoint_id is not None:
            require_identifier(self.awaiting_checkpoint_id, "awaiting_checkpoint_id")
        if not isinstance(self.cancelled, bool):
            fail(FailureCode.INVALID_VALUE, "cancelled must be boolean")


class CampaignStageExecutor(Protocol):
    def run_stage(self, context: CampaignStageExecutionContext) -> CampaignStageResult: ...


class InMemoryCampaignArtifactRepository:
    """Small collision-checking artifact sink for tests and integration smoke."""

    def __init__(self) -> None:
        self._objects: dict[str, CampaignStageResult] = {}

    def put_stage(self, result: CampaignStageResult) -> None:
        key = f"{result.artifact_namespace}/{result.stage_id}"
        existing = self._objects.get(key)
        if existing is not None and existing.artifact_hash != result.artifact_hash:
            fail(FailureCode.ARTIFACT_COLLISION, "Campaign runtime artifact collision", artifact_key=key)
        self._objects[key] = result

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._objects))

    @property
    def hashes(self) -> Mapping[str, str]:
        return MappingProxyType({key.replace("/", "."): value.artifact_hash for key, value in sorted(self._objects.items())})


class DeterministicCampaignStageExecutor:
    """Offline deterministic stage executor used by default tests.

    It mimics the DRAFT/explanation slots a real LLM provider would fill, while
    machine stages produce hash-only control references.  No prompt or trace
    plaintext is persisted.
    """

    def __init__(self, *, failures: Mapping[str, FailureCode] | None = None) -> None:
        self.failures = MappingProxyType(dict(failures or {}))
        self.calls: list[str] = []

    def run_stage(self, context: CampaignStageExecutionContext) -> CampaignStageResult:
        key = stage_key_for(context.route_task.task_id, context.stage_id)
        self.calls.append(key)
        failure = self.failures.get(key)
        if failure is not None:
            return CampaignStageResult(
                route_task_id=context.route_task.task_id,
                desk_id=context.route_task.desk_id,
                stage_id=context.stage_id,
                stage_index=context.stage_index,
                status=StageRuntimeStatus.BLOCKED,
                produced_by=Component.OPERATOR,
                output_hash=None,
                prompt_hash=canonical_hash({"stage_key": key, "prompt": "redacted"}),
                trace_hash=canonical_hash({"stage_key": key, "trace": "failure"}),
                artifact_namespace=context.artifact_namespace,
                failure_code=failure,
                message="stage failed closed",
            )
        status, producer = _status_and_producer_for_stage(context.stage_id)
        material = {
            "campaign_hash": context.campaign.artifact_hash,
            "route_task_id": context.route_task.task_id,
            "desk_id": context.route_task.desk_id,
            "stage_id": context.stage_id,
            "protocol_kind": context.task_protocol.protocol_kind.value,
            "output_state": status.value,
        }
        return CampaignStageResult(
            route_task_id=context.route_task.task_id,
            desk_id=context.route_task.desk_id,
            stage_id=context.stage_id,
            stage_index=context.stage_index,
            status=status,
            produced_by=producer,
            output_hash=canonical_hash(material),
            prompt_hash=canonical_hash({"stage_key": key, "prompt": "hash-only-redacted"}),
            trace_hash=canonical_hash({"stage_key": key, "trace": "hash-only-redacted"}),
            artifact_namespace=context.artifact_namespace,
        )


class ResearchStageControllerFactorExecutor:
    """Adapter that reuses the existing one-Desk ResearchStageController.

    The controller runs the fixed factor protocol as one program-controlled
    serial chain.  The adapter converts the controller result into C5 stage
    records so Campaign routing, stable sorting, and recovery can remain at the
    Campaign layer.
    """

    def __init__(
        self,
        *,
        controller: ResearchStageController,
        question: ResearchQuestionView,
        desks: Mapping[str, DeskLLMConfig],
        providers: Mapping[DeskId, Any],
        tools: Mapping[DeskId, Any],
    ) -> None:
        self.controller = controller
        self.question = question
        self.desks = MappingProxyType(dict(desks))
        self.providers = MappingProxyType(dict(providers))
        self.tools = MappingProxyType(dict(tools))
        self._cached: dict[str, DeskRunResult] = {}

    @property
    def desk_results(self) -> Mapping[str, DeskRunResult]:
        """Return completed/blocked typed Desk results for safe integration projection.

        The values contain structured DRAFT artifacts plus redacted evaluation
        summaries. Provider prompts and raw responses are not retained here.
        """

        return MappingProxyType(dict(sorted(self._cached.items())))

    def run_stage(self, context: CampaignStageExecutionContext) -> CampaignStageResult:
        if context.task_protocol.protocol_kind is not ResearchProtocolKind.FACTOR_EXPLORATION_OR_IMPROVEMENT:
            return DeterministicCampaignStageExecutor().run_stage(context)
        result = self._run_once(context)
        if context.stage_index >= len(result.stage_trace):
            fail(FailureCode.INVALID_STATE, "controller result has no matching stage")
        record = result.stage_trace[context.stage_index]
        status = StageRuntimeStatus.DRAFT if record.status is LLMDeskStatus.DRAFT else StageRuntimeStatus.BLOCKED
        return CampaignStageResult(
            route_task_id=context.route_task.task_id,
            desk_id=context.route_task.desk_id,
            stage_id=context.stage_id,
            stage_index=context.stage_index,
            status=status,
            produced_by=Component.RESEARCH_DESK if status is StageRuntimeStatus.DRAFT else Component.OPERATOR,
            output_hash=record.artifact_hash,
            prompt_hash=canonical_hash({"controller_trace": result.provider_trace.artifact_hash, "stage": context.stage_id}),
            trace_hash=result.provider_trace.artifact_hash,
            artifact_namespace=context.artifact_namespace,
            failure_code=FailureCode(record.failure_code) if record.failure_code else None,
            message="stage failed closed with typed failure code" if record.failure_code else None,
        )

    def _run_once(self, context: CampaignStageExecutionContext) -> DeskRunResult:
        cached = self._cached.get(context.route_task.task_id)
        if cached is not None:
            return cached
        desk_id = DeskId(context.route_task.desk_id)
        desk = self.desks[context.route_task.desk_id]
        provider = self.providers[desk_id]
        tools = self.tools[desk_id]
        result = self.controller.run_desk(desk=desk, question=self.question, provider=provider, tools=tools)
        self._cached[context.route_task.task_id] = result
        return result


class CampaignRoutedLLMRuntime:
    """Run Campaign-selected desks in parallel layers and fixed stage order."""

    def __init__(
        self,
        *,
        config: CampaignRuntimeConfig | None = None,
        repository: InMemoryCampaignArtifactRepository | None = None,
        executor: CampaignStageExecutor | None = None,
    ) -> None:
        self.config = config or CampaignRuntimeConfig()
        self.repository = repository or InMemoryCampaignArtifactRepository()
        self.executor = executor or DeterministicCampaignStageExecutor()

    def run(
        self,
        *,
        run_id: str,
        campaign: ResearchCampaign,
        route: CampaignRoute,
        protocol_plan: CampaignProtocolPlan,
        recovery: CampaignRuntimeRecoveryState | None = None,
        control: CampaignRuntimeControl | None = None,
    ) -> CampaignRuntimeManifest:
        require_identifier(run_id, "run_id")
        _validate_runtime_inputs(campaign=campaign, route=route, protocol_plan=protocol_plan)
        recovery_state = recovery or CampaignRuntimeRecoveryState()
        runtime_control = control or CampaignRuntimeControl()
        if runtime_control.cancel_requested:
            branches = tuple(
                _cancelled_branch(task=task, task_protocol=_protocol_by_task(protocol_plan)[task.task_id])
                for task in sorted(route.tasks, key=lambda item: item.task_id)
            )
            return self._manifest(
                run_id=run_id,
                campaign=campaign,
                route=route,
                protocol_plan=protocol_plan,
                branches=branches,
                status=CampaignRuntimeStatus.CANCELLED,
                cancelled=True,
            )

        task_by_id = {task.task_id: task for task in route.tasks}
        protocol_by_task = _protocol_by_task(protocol_plan)
        pending = set(task_by_id)
        completed: set[str] = set()
        failed_or_blocked: set[str] = set()
        branch_results: dict[str, CampaignBranchResult] = {}

        while pending:
            newly_blocked = sorted(
                task_id for task_id in pending if set(task_by_id[task_id].input_from) & failed_or_blocked
            )
            for task_id in newly_blocked:
                task = task_by_id[task_id]
                branch = _upstream_blocked_branch(task=task, task_protocol=protocol_by_task[task_id])
                branch_results[task_id] = branch
                failed_or_blocked.add(task_id)
                pending.remove(task_id)
            ready = sorted(task_id for task_id in pending if set(task_by_id[task_id].input_from) <= completed)
            if not ready:
                break
            layer_results = self._run_layer(
                campaign=campaign,
                route=route,
                ready_tasks=tuple(task_by_id[task_id] for task_id in ready),
                protocol_by_task=protocol_by_task,
                recovery_state=recovery_state,
            )
            for task_id, branch in sorted(layer_results.items()):
                branch_results[task_id] = branch
                pending.remove(task_id)
                if branch.status is BranchRuntimeStatus.COMPLETED:
                    completed.add(task_id)
                else:
                    failed_or_blocked.add(task_id)
            checkpoint_id = _checkpoint_hit(route, completed)
            if runtime_control.stop_at_next_checkpoint and checkpoint_id is not None and pending:
                return self._manifest(
                    run_id=run_id,
                    campaign=campaign,
                    route=route,
                    protocol_plan=protocol_plan,
                    branches=tuple(branch_results[key] for key in sorted(branch_results)),
                    status=CampaignRuntimeStatus.AWAITING_CHECKPOINT,
                    awaiting_checkpoint_id=checkpoint_id,
                )

        if pending:
            for task_id in sorted(pending):
                branch_results[task_id] = _upstream_blocked_branch(
                    task=task_by_id[task_id],
                    task_protocol=protocol_by_task[task_id],
                )
        branches = tuple(branch_results[key] for key in sorted(branch_results))
        if all(branch.status is BranchRuntimeStatus.COMPLETED for branch in branches):
            status = CampaignRuntimeStatus.COMPLETED
        elif any(branch.status is BranchRuntimeStatus.COMPLETED for branch in branches):
            status = CampaignRuntimeStatus.PARTIAL
        else:
            status = CampaignRuntimeStatus.BLOCKED
        return self._manifest(
            run_id=run_id,
            campaign=campaign,
            route=route,
            protocol_plan=protocol_plan,
            branches=branches,
            status=status,
        )

    def _run_layer(
        self,
        *,
        campaign: ResearchCampaign,
        route: CampaignRoute,
        ready_tasks: tuple[CampaignTaskPlan, ...],
        protocol_by_task: Mapping[str, CampaignTaskProtocolPlan],
        recovery_state: CampaignRuntimeRecoveryState,
    ) -> Mapping[str, CampaignBranchResult]:
        results: dict[str, CampaignBranchResult] = {}
        max_workers = min(self.config.max_parallel_desks, len(ready_tasks))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    self._run_branch,
                    campaign=campaign,
                    route=route,
                    task=task,
                    task_protocol=protocol_by_task[task.task_id],
                    recovery_state=recovery_state,
                ): task.task_id
                for task in ready_tasks
            }
            for future in as_completed(futures):
                task_id = futures[future]
                try:
                    results[task_id] = future.result()
                except ContractValidationError as exc:
                    task = next(item for item in ready_tasks if item.task_id == task_id)
                    results[task_id] = CampaignBranchResult(
                        route_task_id=task.task_id,
                        desk_id=task.desk_id,
                        artifact_namespace=task.artifact_namespace,
                        status=BranchRuntimeStatus.BLOCKED,
                        stage_results=(),
                        failure_code=exc.code,
                        message="branch failed closed with typed failure code",
                    )
                except Exception as exc:  # noqa: BLE001 - isolate one provider/adapter crash to its branch.
                    task = next(item for item in ready_tasks if item.task_id == task_id)
                    results[task_id] = CampaignBranchResult(
                        route_task_id=task.task_id,
                        desk_id=task.desk_id,
                        artifact_namespace=task.artifact_namespace,
                        status=BranchRuntimeStatus.BLOCKED,
                        stage_results=(),
                        failure_code=FailureCode.ADAPTER_EXECUTION_FAILED,
                        message=f"branch executor failed closed: {exc.__class__.__name__}",
                    )
        return MappingProxyType(results)

    def _run_branch(
        self,
        *,
        campaign: ResearchCampaign,
        route: CampaignRoute,
        task: CampaignTaskPlan,
        task_protocol: CampaignTaskProtocolPlan,
        recovery_state: CampaignRuntimeRecoveryState,
    ) -> CampaignBranchResult:
        committed = recovery_state.by_key()
        stage_results: list[CampaignStageResult] = []
        branch_namespace = task.artifact_namespace
        for index, stage_id in enumerate(task_protocol.stage_ids):
            key = stage_key_for(task.task_id, stage_id)
            existing = committed.get(key)
            if existing is not None:
                stage_results.append(existing)
                self.repository.put_stage(existing)
                continue
            context = CampaignStageExecutionContext(
                campaign=campaign,
                route=route,
                route_task=task,
                task_protocol=task_protocol,
                stage_id=stage_id,
                stage_index=index,
                artifact_namespace=f"{branch_namespace}.stage.{index + 1}",
            )
            result = self.executor.run_stage(context)
            self.repository.put_stage(result)
            stage_results.append(result)
            if result.status in {StageRuntimeStatus.BLOCKED, StageRuntimeStatus.CANCELLED, StageRuntimeStatus.SKIPPED}:
                return CampaignBranchResult(
                    route_task_id=task.task_id,
                    desk_id=task.desk_id,
                    artifact_namespace=branch_namespace,
                    status=BranchRuntimeStatus.BLOCKED
                    if result.status is StageRuntimeStatus.BLOCKED
                    else BranchRuntimeStatus.CANCELLED,
                    stage_results=tuple(stage_results),
                    failure_code=result.failure_code,
                    message=result.message or "branch stopped before completion",
                )
        return CampaignBranchResult(
            route_task_id=task.task_id,
            desk_id=task.desk_id,
            artifact_namespace=branch_namespace,
            status=BranchRuntimeStatus.COMPLETED,
            stage_results=tuple(stage_results),
        )

    def _manifest(
        self,
        *,
        run_id: str,
        campaign: ResearchCampaign,
        route: CampaignRoute,
        protocol_plan: CampaignProtocolPlan,
        branches: tuple[CampaignBranchResult, ...],
        status: CampaignRuntimeStatus,
        awaiting_checkpoint_id: str | None = None,
        cancelled: bool = False,
    ) -> CampaignRuntimeManifest:
        artifact_hashes = dict(self.repository.hashes)
        for branch in branches:
            artifact_hashes[f"branch.{branch.route_task_id}"] = branch.artifact_hash
        return CampaignRuntimeManifest(
            run_id=run_id,
            campaign_hash=campaign.artifact_hash,
            route_hash=route.artifact_hash,
            protocol_plan_hash=protocol_plan.artifact_hash,
            runtime_version=CAMPAIGN_RUNTIME_RULE_VERSION,
            status=status,
            branch_results=tuple(sorted(branches, key=lambda item: item.route_task_id)),
            artifact_hashes=artifact_hashes,
            awaiting_checkpoint_id=awaiting_checkpoint_id,
            cancelled=cancelled,
        )


def stage_key_for(route_task_id: str, stage_id: str) -> str:
    require_identifier(route_task_id, "route_task_id")
    require_identifier(stage_id, "stage_id")
    return f"{route_task_id}:{stage_id}"


def _validate_runtime_inputs(
    *,
    campaign: ResearchCampaign,
    route: CampaignRoute,
    protocol_plan: CampaignProtocolPlan,
) -> None:
    require_campaign_executable(campaign)
    if route.campaign_hash != campaign.artifact_hash:
        fail(FailureCode.INVALID_HASH, "route does not bind Campaign hash")
    if protocol_plan.campaign_hash != campaign.artifact_hash or protocol_plan.route_hash != route.artifact_hash:
        fail(FailureCode.INVALID_HASH, "protocol plan does not bind Campaign and route hashes")
    if not (1 <= len(route.selected_desks) <= MAX_CAMPAIGN_DESKS):
        fail(FailureCode.PROVIDER_BUDGET_EXCEEDED, "Campaign runtime requires one to six selected desks")
    task_desks = tuple(task.desk_id for task in route.tasks)
    if set(task_desks) != set(route.selected_desks):
        fail(FailureCode.INVALID_VALUE, "runtime tasks must match selected desks")
    namespaces = tuple(task.artifact_namespace for task in route.tasks)
    if len(set(namespaces)) != len(namespaces):
        fail(FailureCode.ARTIFACT_COLLISION, "Campaign route contains duplicate artifact namespaces")
    protocol_tasks = {item.route_task_id for item in protocol_plan.task_protocols}
    if protocol_tasks != {task.task_id for task in route.tasks}:
        fail(FailureCode.INVALID_VALUE, "protocol plan tasks must exactly match route tasks")


def _protocol_by_task(protocol_plan: CampaignProtocolPlan) -> Mapping[str, CampaignTaskProtocolPlan]:
    return MappingProxyType({item.route_task_id: item for item in protocol_plan.task_protocols})


def _checkpoint_hit(route: CampaignRoute, completed_task_ids: set[str]) -> str | None:
    for checkpoint in route.checkpoint_plan:
        if checkpoint.required and checkpoint.after_task_ids and set(checkpoint.after_task_ids) <= completed_task_ids:
            return checkpoint.checkpoint_id
    return None


def _cancelled_branch(*, task: CampaignTaskPlan, task_protocol: CampaignTaskProtocolPlan) -> CampaignBranchResult:
    result = CampaignStageResult(
        route_task_id=task.task_id,
        desk_id=task.desk_id,
        stage_id=task_protocol.stage_ids[0],
        stage_index=0,
        status=StageRuntimeStatus.CANCELLED,
        produced_by=Component.OPERATOR,
        output_hash=None,
        prompt_hash=None,
        trace_hash=None,
        artifact_namespace=f"{task.artifact_namespace}.cancelled",
        failure_code=FailureCode.EVOLUTION_CANCELLED,
        message="campaign cancellation requested before branch start",
    )
    return CampaignBranchResult(
        route_task_id=task.task_id,
        desk_id=task.desk_id,
        artifact_namespace=task.artifact_namespace,
        status=BranchRuntimeStatus.CANCELLED,
        stage_results=(result,),
        failure_code=FailureCode.EVOLUTION_CANCELLED,
        message="campaign cancellation requested",
    )


def _upstream_blocked_branch(*, task: CampaignTaskPlan, task_protocol: CampaignTaskProtocolPlan) -> CampaignBranchResult:
    result = CampaignStageResult(
        route_task_id=task.task_id,
        desk_id=task.desk_id,
        stage_id=task_protocol.stage_ids[0],
        stage_index=0,
        status=StageRuntimeStatus.SKIPPED,
        produced_by=Component.OPERATOR,
        output_hash=None,
        prompt_hash=None,
        trace_hash=None,
        artifact_namespace=f"{task.artifact_namespace}.upstream_blocked",
        failure_code=FailureCode.UPSTREAM_BLOCKED,
        message="upstream dependency blocked",
    )
    return CampaignBranchResult(
        route_task_id=task.task_id,
        desk_id=task.desk_id,
        artifact_namespace=task.artifact_namespace,
        status=BranchRuntimeStatus.SKIPPED_UPSTREAM,
        stage_results=(result,),
        failure_code=FailureCode.UPSTREAM_BLOCKED,
        message="upstream dependency blocked",
    )


def _status_and_producer_for_stage(stage_id: str) -> tuple[StageRuntimeStatus, Component]:
    lowered = stage_id.lower()
    if any(token in lowered for token in _FINAL_EXPLANATION_STAGES):
        return StageRuntimeStatus.EXPLANATION, Component.RESEARCH_DESK
    if any(token in lowered for token in _CONTROL_STAGES):
        return StageRuntimeStatus.CONTROL_REFERENCE, Component.CRITIC_SERVICE
    return StageRuntimeStatus.DRAFT, Component.RESEARCH_DESK


__all__ = [
    "CAMPAIGN_RUNTIME_RULE_VERSION",
    "BranchRuntimeStatus",
    "CampaignBranchResult",
    "CampaignCommittedStage",
    "CampaignRoutedLLMRuntime",
    "CampaignRuntimeConfig",
    "CampaignRuntimeControl",
    "CampaignRuntimeManifest",
    "CampaignRuntimeRecoveryState",
    "CampaignRuntimeStatus",
    "CampaignStageExecutionContext",
    "CampaignStageExecutor",
    "CampaignStageResult",
    "DeterministicCampaignStageExecutor",
    "InMemoryCampaignArtifactRepository",
    "ResearchStageControllerFactorExecutor",
    "StageRuntimeStatus",
    "stage_key_for",
]
