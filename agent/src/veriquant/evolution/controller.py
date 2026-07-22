"""Budgeted, resumable DRAFT candidate generation with no promotion authority."""

from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from statistics import fmean
from threading import Event, Lock, RLock
from typing import Any, Callable, Mapping, Protocol, Sequence

from ..adapters import (
    DraftProposal,
    ProposalContext,
    QuantaAlphaProposalAdapter,
)
from ..contracts import (
    Component,
    ContractValidationError,
    EvolutionLineage,
    FailureCode,
    ResearchState,
    validate_component_payload,
)
from ..data import canonical_content_hash
from ..evidence import (
    AttemptController,
    ExperimentIdentity,
    StartedAttempt,
    TrialLedger,
    TrialOutcome,
    TrialStatus,
)
from .memory import ProposalMemory
from .models import (
    EvolutionBatch,
    EvolutionChildResult,
    EvolutionLimits,
    EvolutionOperator,
    EvolutionPromptContext,
    EvolutionRequest,
    FrozenIdentityInputs,
    GateOutcome,
    GateResult,
    VisibleParentEvidence,
)
from .scoring import ParentSelector


_FORBIDDEN_CONTEXT_KEYS = frozenset(
    {
        "sealed",
        "sealedmetrics",
        "sealedreference",
        "testmetrics",
        "holdoutmetrics",
        "officialmetrics",
        "promotion",
        "promotiondecision",
        "replacebest",
    }
)
_FORBIDDEN_CONTEXT_TEXT = (
    "sealed metric",
    "sealed rank",
    "sealed result",
    "holdout metric",
    "test metric",
    "replace best",
)
_NORMALIZE_KEY = re.compile(r"[^a-z0-9]+")


def _reject_privileged_generation_payload(value: Any, path: str = "payload") -> None:
    if callable(value):
        raise ContractValidationError(
            FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
            "generation payload cannot contain a callable",
            details={"path": path},
        )
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise ContractValidationError(
                    FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
                    "generation payload keys must be strings",
                    details={"path": path},
                )
            normalized = _NORMALIZE_KEY.sub("", raw_key.lower())
            if normalized in _FORBIDDEN_CONTEXT_KEYS:
                raise ContractValidationError(
                    FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
                    "generation payload carries privileged authority or evidence",
                    details={"path": f"{path}.{raw_key}"},
                )
            _reject_privileged_generation_payload(child, f"{path}.{raw_key}")
    elif isinstance(value, (tuple, list)):
        for index, child in enumerate(value):
            _reject_privileged_generation_payload(child, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        if any(token in lowered for token in _FORBIDDEN_CONTEXT_TEXT):
            raise ContractValidationError(
                FailureCode.EVOLUTION_CONTEXT_REJECTED,
                "generation text exposes forbidden research evidence",
                details={"path": path},
            )
    elif value is not None and not isinstance(value, (int, float, bool)):
        raise ContractValidationError(
            FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD,
            "generation payload contains an unsupported value",
            details={"path": path},
        )


class EvolutionProposalProvider(Protocol):
    """Provider sees only EvolutionRequest and returns an untrusted static mapping."""

    def propose(self, *, request: EvolutionRequest) -> Mapping[str, Any]:
        ...


class CandidateGate(Protocol):
    """External parse/PIT/evaluation status adapter; it cannot promote."""

    def assess(self, *, proposal: DraftProposal, request: EvolutionRequest) -> GateResult:
        ...


class DraftCandidateGate:
    """Default offline gate stops after adapter parse and keeps the candidate DRAFT."""

    def assess(self, *, proposal: DraftProposal, request: EvolutionRequest) -> GateResult:
        if proposal.state is not ResearchState.DRAFT:
            return GateResult(
                outcome=GateOutcome.EVALUATION_REJECTION,
                failure_code=FailureCode.INVALID_STATE,
            )
        return GateResult(outcome=GateOutcome.READY_DRAFT)


class DeterministicEvolutionProvider:
    """Small offline original/mutation/crossover templates; no LLM or network."""

    def __init__(self, *, feature_name: str = "close") -> None:
        if not isinstance(feature_name, str) or not feature_name:
            raise ValueError("feature_name must be non-empty")
        self.feature_name = feature_name

    def propose(self, *, request: EvolutionRequest) -> Mapping[str, Any]:
        operator = request.context.operator
        field = self.feature_name
        if operator is EvolutionOperator.ORIGINAL:
            expression = f"rank_cross_section(pct_change({field}, 20))"
            mechanism = "Visible underreaction motivates a fixed trailing-return draft."
        elif operator is EvolutionOperator.MUTATION:
            expression = f"zscore_cross_section(pct_change({field}, 20))"
            mechanism = (
                "A bounded transform mutation of the selected visible parent may alter "
                "cross-sectional scaling."
            )
        else:
            expression = (
                f"add(rank_cross_section(pct_change({field}, 20)), "
                f"zscore_cross_section(pct_change({field}, 10)))"
            )
            mechanism = (
                "A bounded crossover combines two fixed trailing-return transforms "
                "using the selected visible parents."
            )
        suffix = hashlib.sha256(request.child_id.encode("utf-8")).hexdigest()[:12]
        return {
            "hypothesis": {
                "hypothesis_id": f"evo-hyp-{suffix}",
                "statement": (
                    f"{operator.value.title()} candidate {request.child_id} may retain "
                    "short-horizon relative continuation."
                ),
                "economic_mechanism": mechanism,
                "expected_direction": "positive",
                "falsification_tests": [
                    "Reject when permitted visible net and stability evidence fails."
                ],
            },
            "factor_task": {
                "draft_id": f"evo-factor-{suffix}",
                "factor_name": f"evo_{operator.value.lower()}_{suffix}",
                "factor_description": "Restricted deterministic evolution draft.",
                "factor_formulation": expression,
                "expression": expression,
                "variables": [field],
                "lookback_bars": 20,
                "neutralization": [],
            },
        }


class GenerationIdentityAuthority:
    """Separate deterministic identity factory; it computes no metrics or verdict."""

    authority_component = Component.EVIDENCE_SERVICE

    def __init__(self, frozen: FrozenIdentityInputs) -> None:
        if not isinstance(frozen, FrozenIdentityInputs):
            raise TypeError("frozen identity inputs are required")
        self.frozen = frozen

    def create(self, *, request: EvolutionRequest) -> ExperimentIdentity:
        context = request.context
        return ExperimentIdentity(
            experiment_id=request.child_id,
            dataset_hash=self.frozen.dataset_hash,
            universe_hash=self.frozen.universe_hash,
            factor_hash=context.prompt_hash,
            split_hash=self.frozen.split_hash,
            label_hash=self.frozen.label_hash,
            cost_hash=self.frozen.cost_hash,
            code_hash=self.frozen.code_hash,
            availability_hash=self.frozen.availability_hash,
            seed=context.deterministic_seed,
        )


class CancellationToken:
    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class EvolutionController:
    """Original/mutation/crossover generator bounded by the immutable Trial Ledger."""

    def __init__(
        self,
        *,
        family_id: str,
        proposer_id: str,
        ledger: TrialLedger,
        memory: ProposalMemory,
        limits: EvolutionLimits,
        proposal_context: ProposalContext,
        identity_authority: GenerationIdentityAuthority,
        seed: int = 7,
        provider: EvolutionProposalProvider | None = None,
        gate: CandidateGate | None = None,
        selector: ParentSelector | None = None,
        adapter: QuantaAlphaProposalAdapter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "evolution seed must be a non-negative integer",
            )
        self.family_id = family_id
        self.proposer_id = proposer_id
        self.ledger = ledger
        self.memory = memory
        self.limits = limits
        self.proposal_context = proposal_context
        self.identity_authority = identity_authority
        self.seed = seed
        self.provider = provider or DeterministicEvolutionProvider()
        self.gate = gate or DraftCandidateGate()
        self.selector = selector or ParentSelector()
        self.adapter = adapter or QuantaAlphaProposalAdapter(
            max_candidates=limits.max_children
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.attempts = AttemptController(ledger)
        self.ledger.create_family(family_id, limits.family_budget)
        self._locks_guard = RLock()
        self._child_locks: dict[str, Lock] = {}

    def _child_lock(self, child_id: str) -> Lock:
        with self._locks_guard:
            return self._child_locks.setdefault(child_id, Lock())

    def _seed_for(self, context_hash: str) -> int:
        digest = hashlib.sha256(f"{self.seed}:{context_hash}".encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    def _context(
        self,
        *,
        operator: EvolutionOperator,
        generation: int,
        revision: int,
        child_index: int,
        parents,
    ) -> EvolutionPromptContext:
        preliminary = EvolutionPromptContext(
            operator=operator,
            generation=generation,
            revision=revision,
            child_index=child_index,
            parent_scores=parents,
            deterministic_seed=0,
        )
        return EvolutionPromptContext(
            operator=operator,
            generation=generation,
            revision=revision,
            child_index=child_index,
            parent_scores=parents,
            deterministic_seed=self._seed_for(preliminary.artifact_hash),
        )

    def _request(
        self,
        *,
        operator: EvolutionOperator,
        generation: int,
        revision: int,
        child_index: int,
        parents,
    ) -> EvolutionRequest:
        context = self._context(
            operator=operator,
            generation=generation,
            revision=revision,
            child_index=child_index,
            parents=parents,
        )
        plan_hash = canonical_content_hash(
            {
                "family_id": self.family_id,
                "operator": operator.value,
                "generation": generation,
                "revision": revision,
                "child_index": child_index,
                "parent_ids": context.parent_ids,
                "prompt_hash": context.prompt_hash,
                "seed": self.seed,
            }
        )
        child_id = f"evo-{plan_hash.removeprefix('sha256:')[:24]}"
        return EvolutionRequest(
            child_id=child_id,
            family_id=self.family_id,
            context=context,
        )

    def generate(
        self,
        *,
        operator: EvolutionOperator,
        generation: int,
        count: int,
        parent_evidence: Sequence[VisibleParentEvidence] = (),
        revision: int = 0,
        max_workers: int | None = None,
        cancellation: CancellationToken | None = None,
    ) -> EvolutionBatch:
        if not isinstance(operator, EvolutionOperator):
            raise ContractValidationError(FailureCode.INVALID_VALUE, "invalid operator")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 0 <= generation < self.limits.max_generations
        ):
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "generation exceeds max_generations",
            )
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision <= self.limits.max_revisions
        ):
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "revision exceeds max_revisions",
            )
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= self.limits.max_children
        ):
            raise ContractValidationError(
                FailureCode.INVALID_VALUE,
                "child count exceeds max_children",
            )
        if operator is EvolutionOperator.ORIGINAL and parent_evidence:
            raise ContractValidationError(
                FailureCode.EVOLUTION_CONTEXT_REJECTED,
                "original generation cannot receive parent evidence",
            )
        parents = self.selector.select(
            parent_evidence,
            count=operator.parent_count,
        )
        requests = tuple(
            self._request(
                operator=operator,
                generation=generation,
                revision=revision,
                child_index=index,
                parents=parents,
            )
            for index in range(count)
        )
        token = cancellation or CancellationToken()
        workers = max_workers or min(4, count)
        if workers < 1:
            raise ContractValidationError(FailureCode.INVALID_VALUE, "max_workers is invalid")
        if workers == 1:
            children = tuple(self._generate_one(request, token) for request in requests)
        else:
            with ThreadPoolExecutor(max_workers=min(workers, count)) as pool:
                children = tuple(pool.map(lambda req: self._generate_one(req, token), requests))
        return EvolutionBatch(
            operator=operator,
            generation=generation,
            children=children,
        )

    def _resume(self, request: EvolutionRequest) -> EvolutionChildResult | None:
        stored = self.memory.get(request.child_id)
        if stored is None:
            return None
        replay = self.ledger.replay()
        trial = replay.trials.get(stored.lineage.trial_id)
        if trial is None:
            raise ContractValidationError(
                FailureCode.LINEAGE_CORRUPTION,
                "lineage has no Trial Ledger reservation",
            )
        expected_outcome, expected_failure = self._ledger_terminal(stored.lineage)
        if trial.status is TrialStatus.STARTED:
            self.ledger.commit_outcome(
                trial.trial_id,
                outcome=expected_outcome,
                failure_code=expected_failure,
            )
        elif (
            trial.status is not TrialStatus.TERMINAL
            or trial.outcome is not expected_outcome
            or trial.failure_code is not expected_failure
        ):
            raise ContractValidationError(
                FailureCode.LINEAGE_CORRUPTION,
                "lineage and Trial Ledger terminal state disagree",
            )
        return EvolutionChildResult(
            child_id=request.child_id,
            lineage=stored.lineage,
            proposal=stored.proposal,
            failure_code=stored.lineage.failure_code,
            resumed=True,
        )

    @staticmethod
    def _ledger_terminal(
        lineage: EvolutionLineage,
    ) -> tuple[TrialOutcome, FailureCode | None]:
        mapping = {
            "COMMITTED_DRAFT": TrialOutcome.SUCCESS,
            "GENERATION_FAILED": TrialOutcome.EXCEPTION,
            "PARSE_FAILED": TrialOutcome.PARSE_REJECTION,
            "PIT_FAILED": TrialOutcome.PIT_REJECTION,
            "EVALUATION_FAILED": TrialOutcome.EVALUATION_REJECTION,
            "TIMEOUT": TrialOutcome.TIMEOUT,
            "CANCELLED": TrialOutcome.CANCELLATION,
        }
        return mapping[lineage.outcome], lineage.failure_code

    def _failure_lineage(
        self,
        *,
        request: EvolutionRequest,
        attempt: StartedAttempt,
        outcome: str,
        failure_code: FailureCode,
    ) -> EvolutionLineage:
        failure_hash = canonical_content_hash(
            {
                "child_id": request.child_id,
                "prompt_hash": request.context.prompt_hash,
                "outcome": outcome,
                "failure_code": failure_code.value,
            }
        )
        return EvolutionLineage(
            lineage_id=f"lineage-{request.child_id}",
            candidate_id=request.child_id,
            candidate_hash=failure_hash,
            parent_ids=request.context.parent_ids,
            operator=request.context.operator.value,
            generation=request.context.generation,
            prompt_hash=request.context.prompt_hash,
            novelty=0.0,
            budget_reservation_id=attempt.reserve_event.event_id,
            trial_id=attempt.trial_id,
            seed=request.context.deterministic_seed,
            outcome=outcome,
            failure_code=failure_code,
            created_at=self.clock(),
            state=ResearchState.BLOCKED,
        )

    def _commit_failure(
        self,
        *,
        request: EvolutionRequest,
        attempt: StartedAttempt,
        lineage_outcome: str,
        trial_outcome: TrialOutcome,
        failure_code: FailureCode,
    ) -> EvolutionChildResult:
        lineage = self._failure_lineage(
            request=request,
            attempt=attempt,
            outcome=lineage_outcome,
            failure_code=failure_code,
        )
        self.memory.record(lineage, None)
        self.attempts.commit_failure(
            attempt,
            outcome=trial_outcome,
            failure_code=failure_code,
        )
        return EvolutionChildResult(
            child_id=request.child_id,
            lineage=lineage,
            proposal=None,
            failure_code=failure_code,
        )

    def _novelty(self, request: EvolutionRequest, proposal: DraftProposal) -> float:
        if request.context.operator is EvolutionOperator.ORIGINAL:
            return 1.0
        if proposal.artifact_hash in {
            score.candidate_hash for score in request.context.parent_scores
        }:
            return 0.0
        inherited = fmean(score.novelty for score in request.context.parent_scores)
        return min(1.0, 0.5 + 0.5 * inherited)

    def _generate_one(
        self, request: EvolutionRequest, cancellation: CancellationToken
    ) -> EvolutionChildResult:
        with self._child_lock(request.child_id):
            resumed = self._resume(request)
            if resumed is not None:
                return resumed
            if cancellation.cancelled:
                return EvolutionChildResult(
                    child_id=request.child_id,
                    lineage=None,
                    proposal=None,
                    failure_code=FailureCode.EVOLUTION_CANCELLED,
                    started=False,
                )
            identity = self.identity_authority.create(request=request)
            try:
                attempt = self.attempts.start(
                    family_id=self.family_id,
                    trial_id=f"trial-{request.child_id}",
                    identity=identity,
                    proposer_id=self.proposer_id,
                )
            except ContractValidationError as exc:
                if exc.code is FailureCode.INVALID_VALUE and "budget" in str(exc).lower():
                    return EvolutionChildResult(
                        child_id=request.child_id,
                        lineage=None,
                        proposal=None,
                        failure_code=FailureCode.EVOLUTION_BUDGET_EXHAUSTED,
                        started=False,
                    )
                raise
            if cancellation.cancelled:
                return self._commit_failure(
                    request=request,
                    attempt=attempt,
                    lineage_outcome="CANCELLED",
                    trial_outcome=TrialOutcome.CANCELLATION,
                    failure_code=FailureCode.EVOLUTION_CANCELLED,
                )
            try:
                payload = self.provider.propose(request=request)
                if not isinstance(payload, Mapping):
                    raise ContractValidationError(
                        FailureCode.PROVIDER_SCHEMA_REJECTED,
                        "evolution provider must return a mapping",
                    )
                _reject_privileged_generation_payload(payload)
                validate_component_payload(Component.QUANTAALPHA, payload)
                proposal = self.adapter.adapt(payload, context=self.proposal_context)
            except TimeoutError:
                return self._commit_failure(
                    request=request,
                    attempt=attempt,
                    lineage_outcome="TIMEOUT",
                    trial_outcome=TrialOutcome.TIMEOUT,
                    failure_code=FailureCode.PROVIDER_TIMEOUT,
                )
            except ContractValidationError as exc:
                return self._commit_failure(
                    request=request,
                    attempt=attempt,
                    lineage_outcome="PARSE_FAILED",
                    trial_outcome=TrialOutcome.PARSE_REJECTION,
                    failure_code=exc.code,
                )
            except Exception:
                return self._commit_failure(
                    request=request,
                    attempt=attempt,
                    lineage_outcome="GENERATION_FAILED",
                    trial_outcome=TrialOutcome.EXCEPTION,
                    failure_code=FailureCode.ADAPTER_EXECUTION_FAILED,
                )

            if cancellation.cancelled:
                return self._commit_failure(
                    request=request,
                    attempt=attempt,
                    lineage_outcome="CANCELLED",
                    trial_outcome=TrialOutcome.CANCELLATION,
                    failure_code=FailureCode.EVOLUTION_CANCELLED,
                )
            assessment = self.gate.assess(proposal=proposal, request=request)
            if not isinstance(assessment, GateResult):
                return self._commit_failure(
                    request=request,
                    attempt=attempt,
                    lineage_outcome="EVALUATION_FAILED",
                    trial_outcome=TrialOutcome.EVALUATION_REJECTION,
                    failure_code=FailureCode.INVALID_VALUE,
                )
            if assessment.outcome is GateOutcome.PIT_REJECTION:
                return self._commit_failure(
                    request=request,
                    attempt=attempt,
                    lineage_outcome="PIT_FAILED",
                    trial_outcome=TrialOutcome.PIT_REJECTION,
                    failure_code=assessment.failure_code or FailureCode.INVALID_TIME_ORDER,
                )
            if assessment.outcome is GateOutcome.EVALUATION_REJECTION:
                return self._commit_failure(
                    request=request,
                    attempt=attempt,
                    lineage_outcome="EVALUATION_FAILED",
                    trial_outcome=TrialOutcome.EVALUATION_REJECTION,
                    failure_code=assessment.failure_code or FailureCode.INVALID_VALUE,
                )
            lineage = EvolutionLineage(
                lineage_id=f"lineage-{request.child_id}",
                candidate_id=request.child_id,
                candidate_hash=proposal.artifact_hash,
                parent_ids=request.context.parent_ids,
                operator=request.context.operator.value,
                generation=request.context.generation,
                prompt_hash=request.context.prompt_hash,
                novelty=self._novelty(request, proposal),
                budget_reservation_id=attempt.reserve_event.event_id,
                trial_id=attempt.trial_id,
                seed=request.context.deterministic_seed,
                outcome="COMMITTED_DRAFT",
                failure_code=None,
                created_at=self.clock(),
                state=ResearchState.DRAFT,
            )
            self.memory.record(lineage, proposal)
            self.attempts.commit_success(attempt)
            return EvolutionChildResult(
                child_id=request.child_id,
                lineage=lineage,
                proposal=proposal,
                failure_code=None,
            )
