"""Six-Desk outer DAG, mechanical Evidence Court, aggregation, and selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ..contracts import CanonicalContract, ContractValidationError, FailureCode, ResearchQuestion, ResearchState
from ..contracts.base import require_identifier
from ..evidence import (
    CourtOutcome,
    CriticFinding,
    EvidenceCourt,
    MechanicalDecision,
)
from .artifacts import BranchArtifactStore
from .authority import DeterministicOfficialEvidenceAuthority
from .dag import (
    DAGRunResult,
    IncompleteTask,
    ResearchDAGRuntime,
    ResearchTaskDAG,
    ResearchTaskSpec,
    TaskExecution,
)
from .models import (
    DeskSpec,
    ReadOnlyToolCatalog,
    TaskStatus,
    build_default_desks,
    build_default_tool_catalog,
)
from .pipeline import BranchRunResult, ResearchBranchStateMachine
from .providers import DeterministicProposalProvider, ResearchProposalProvider


_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def _fail(code: FailureCode, message: str, **details) -> None:
    raise ContractValidationError(code, message, details=details)


@dataclass(frozen=True, kw_only=True)
class CourtBranchResult(CanonicalContract):
    branch_id: str
    branch: BranchRunResult
    findings: tuple[CriticFinding, ...]
    decision: MechanicalDecision

    def __post_init__(self) -> None:
        require_identifier(self.branch_id, "branch_id")
        if self.branch.branch_id != self.branch_id:
            _fail(FailureCode.INVALID_VALUE, "court result branch mismatch")
        if self.decision.evidence_hash != self.branch.evaluation.evidence.artifact_hash:
            _fail(FailureCode.INVALID_HASH, "court decision does not bind branch evidence")


@dataclass(frozen=True, kw_only=True)
class ValidatedAggregation(CanonicalContract):
    run_id: str
    branches: tuple[CourtBranchResult, ...]

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        if not self.branches:
            _fail(FailureCode.INCOMPLETE_RESULT, "validated aggregation cannot be empty")
        if any(
            item.decision.outcome is not CourtOutcome.VALIDATED_RESEARCH
            for item in self.branches
        ):
            _fail(FailureCode.INVALID_STATE, "aggregation contains a non-validated branch")


@dataclass(frozen=True, kw_only=True)
class SelectionDecision(CanonicalContract):
    run_id: str
    selected_branch_id: str
    ranked_branch_ids: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    controller_id: str = "selection-controller-v1"

    def __post_init__(self) -> None:
        require_identifier(self.run_id, "run_id")
        require_identifier(self.selected_branch_id, "selected_branch_id")
        if not self.ranked_branch_ids or self.ranked_branch_ids[0] != self.selected_branch_id:
            _fail(FailureCode.INVALID_VALUE, "selection ranking must begin with selected branch")
        if len(self.ranked_branch_ids) != len(self.evidence_hashes):
            _fail(FailureCode.INVALID_VALUE, "selection ranking/evidence lengths differ")


class SelectionController:
    """Visible-evidence mechanical ranking; no evolution or sealed input."""

    controller_id = "selection-controller-v1"

    def select(self, aggregation: ValidatedAggregation) -> SelectionDecision:
        ranked = tuple(
            sorted(
                aggregation.branches,
                key=lambda item: (
                    -item.branch.evaluation.evidence.metrics.net_return,
                    -item.branch.evaluation.evidence.metrics.rank_ic,
                    -item.branch.evaluation.evidence.metrics.incremental_ic_mean,
                    item.branch.evaluation.evidence.metrics.max_registered_abs_correlation,
                    item.branch_id,
                ),
            )
        )
        return SelectionDecision(
            run_id=aggregation.run_id,
            selected_branch_id=ranked[0].branch_id,
            ranked_branch_ids=tuple(item.branch_id for item in ranked),
            evidence_hashes=tuple(item.branch.evaluation.evidence.artifact_hash for item in ranked),
        )


@dataclass(frozen=True, kw_only=True)
class ResearchRunResult:
    run_id: str
    run_dir: Path
    dag_result: DAGRunResult
    branch_directories: Mapping[str, Path]


ProviderFactory = Callable[[DeskSpec], ResearchProposalProvider]


class ResearchDAGController:
    def __init__(
        self,
        *,
        desks: tuple[DeskSpec, ...] | None = None,
        tool_catalog: ReadOnlyToolCatalog | None = None,
        authority: DeterministicOfficialEvidenceAuthority | None = None,
        provider_factory: ProviderFactory | None = None,
        evidence_court: EvidenceCourt | None = None,
        selection_controller: SelectionController | None = None,
        artifact_root: str | Path | None = None,
        max_workers: int = 6,
    ) -> None:
        self.desks = desks or build_default_desks()
        expected = {
            "fundamental",
            "expectations_events",
            "market_structure",
            "alternative_data",
            "macro_regime",
            "meta_alpha",
        }
        if {desk.desk_id.value for desk in self.desks} != expected or len(self.desks) != 6:
            _fail(FailureCode.INVALID_VALUE, "controller requires exactly the six declared Research Desks")
        if len({desk.artifact_namespace for desk in self.desks}) != 6:
            _fail(FailureCode.INVALID_VALUE, "desk artifact namespaces must be isolated")
        if len({id(desk.budget) for desk in self.desks}) != 6:
            _fail(FailureCode.INVALID_VALUE, "desk budget objects must not be shared")
        self.tool_catalog = tool_catalog or build_default_tool_catalog()
        self.authority = authority or DeterministicOfficialEvidenceAuthority()
        self.provider_factory = provider_factory or (lambda desk: DeterministicProposalProvider())
        self.evidence_court = evidence_court or EvidenceCourt()
        self.selection_controller = selection_controller or SelectionController()
        default_root = Path(__file__).resolve().parents[4] / ".autobuild" / "veriquant" / "runs"
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else default_root
        self.runtime = ResearchDAGRuntime(max_workers=max_workers)

    def _build_dag(self) -> ResearchTaskDAG:
        branch_tasks = tuple(f"branch.{desk.desk_id.value}" for desk in self.desks)
        court_tasks = tuple(f"court.{desk.desk_id.value}" for desk in self.desks)
        tasks: list[ResearchTaskSpec] = [ResearchTaskSpec(task_id="question")]
        tasks.extend(
            ResearchTaskSpec(
                task_id=task_id,
                depends_on=("question",),
                input_from={"question": "question"},
            )
            for task_id in branch_tasks
        )
        tasks.extend(
            ResearchTaskSpec(
                task_id=court_task,
                depends_on=(branch_task,),
                input_from={"branch": branch_task},
            )
            for court_task, branch_task in zip(court_tasks, branch_tasks)
        )
        tasks.append(
            ResearchTaskSpec(
                task_id="validated_aggregation",
                depends_on=court_tasks,
                input_from={desk.desk_id.value: task for desk, task in zip(self.desks, court_tasks)},
            )
        )
        tasks.append(
            ResearchTaskSpec(
                task_id="selection",
                depends_on=("validated_aggregation",),
                input_from={"aggregation": "validated_aggregation"},
            )
        )
        return ResearchTaskDAG(tuple(tasks))

    def run(self, *, run_id: str, question: ResearchQuestion) -> ResearchRunResult:
        if not isinstance(question, ResearchQuestion) or question.state is not ResearchState.DRAFT:
            _fail(FailureCode.INVALID_STATE, "Research DAG requires a DRAFT ResearchQuestion")
        if _SAFE_RUN_ID.fullmatch(run_id) is None:
            _fail(FailureCode.INVALID_IDENTIFIER, "run_id is not a safe artifact path segment")
        run_dir = (self.artifact_root / run_id).resolve()
        expected_parent = self.artifact_root.resolve()
        if run_dir.parent != expected_parent:
            _fail(FailureCode.INVALID_IDENTIFIER, "run directory escaped artifact root")
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise ContractValidationError(
                FailureCode.ARTIFACT_COLLISION,
                "Research DAG run namespace already exists",
            ) from exc
        self.authority.prepare_run(run_id=run_id, run_dir=run_dir, desks=self.desks)
        stores = {
            desk.desk_id.value: BranchArtifactStore(
                artifact_root=self.artifact_root,
                run_id=run_id,
                branch_id=desk.artifact_namespace,
                event_time=question.decision_time,
            )
            for desk in self.desks
        }
        registries = {
            desk.desk_id.value: self.tool_catalog.for_desk(desk) for desk in self.desks
        }
        providers = {desk.desk_id.value: self.provider_factory(desk) for desk in self.desks}
        handlers = {"question": lambda inputs: TaskExecution(status=TaskStatus.COMPLETED, output=question)}

        for desk in self.desks:
            desk_id = desk.desk_id.value
            branch_task = f"branch.{desk_id}"
            court_task = f"court.{desk_id}"

            def branch_handler(inputs, *, _desk=desk, _desk_id=desk_id):
                machine = ResearchBranchStateMachine(
                    run_id=run_id,
                    desk=_desk,
                    question=inputs["question"],
                    provider=providers[_desk_id],
                    tools=registries[_desk_id],
                    store=stores[_desk_id],
                    authority=self.authority,
                )
                execution = machine.run()
                if execution.status is TaskStatus.COMPLETED:
                    return TaskExecution(status=TaskStatus.COMPLETED, output=execution.result)
                return TaskExecution(
                    status=execution.status,
                    failure_code=execution.failure_code,
                    error=execution.message,
                )

            def court_handler(inputs, *, _desk=desk, _desk_id=desk_id):
                branch = inputs["branch"]
                findings = self.evidence_court.findings(
                    branch.evaluation.evidence,
                    pit_valid=branch.calculation.pit_valid,
                )
                decision = self.evidence_court.decide(
                    evidence=branch.evaluation.evidence,
                    identity_hash=branch.evaluation.manifest.identity_hash,
                    proposer_id=f"desk:{_desk_id}",
                    approver_id="evidence-court-controller",
                    decided_at=self.authority.dataset.evaluation_time,
                    pit_valid=branch.calculation.pit_valid,
                    sealed_passed=None,
                )
                result = CourtBranchResult(
                    branch_id=_desk_id,
                    branch=branch,
                    findings=findings,
                    decision=decision,
                )
                stores[_desk_id].write_artifact(
                    "critic_findings.json",
                    {"findings": findings, "decision": decision},
                )
                stores[_desk_id].append_event(
                    stage="evidence_court",
                    status=decision.outcome.value,
                    event_time=self.authority.dataset.evaluation_time,
                )
                return TaskExecution(status=TaskStatus.COMPLETED, output=result)

            handlers[branch_task] = branch_handler
            handlers[court_task] = court_handler

        def aggregate_handler(inputs):
            validated = tuple(
                sorted(
                    (
                        item
                        for item in inputs.values()
                        if item.decision.outcome is CourtOutcome.VALIDATED_RESEARCH
                    ),
                    key=lambda item: item.branch_id,
                )
            )
            if not validated:
                raise IncompleteTask("Evidence Court produced no VALIDATED_RESEARCH branch")
            return TaskExecution(
                status=TaskStatus.COMPLETED,
                output=ValidatedAggregation(run_id=run_id, branches=validated),
            )

        def selection_handler(inputs):
            return TaskExecution(
                status=TaskStatus.COMPLETED,
                output=self.selection_controller.select(inputs["aggregation"]),
            )

        handlers["validated_aggregation"] = aggregate_handler
        handlers["selection"] = selection_handler
        result = self.runtime.run(self._build_dag(), handlers=handlers)
        return ResearchRunResult(
            run_id=run_id,
            run_dir=run_dir,
            dag_result=result,
            branch_directories={desk_id: store.branch_dir for desk_id, store in stores.items()},
        )
