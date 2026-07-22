from __future__ import annotations

from threading import Lock

import pytest

from src.veriquant.adapters import (
    ProposalContext,
)
from src.veriquant.contracts import (
    ContractValidationError,
    FailureCode,
    ResearchState,
)
from src.veriquant.data import FieldRole, canonical_content_hash
from src.veriquant.evidence import TrialLedger, TrialOutcome, TrialStatus
from src.veriquant.evolution import (
    CancellationToken,
    DeterministicEvolutionProvider,
    EvolutionController,
    EvolutionLimits,
    EvolutionOperator,
    FrozenIdentityInputs,
    GateOutcome,
    GateResult,
    GenerationIdentityAuthority,
    LineageStore,
    ProposalMemory,
    VisibleParentEvidence,
)
from datetime import datetime, timezone


NOW = datetime(2024, 8, 1, 12, 0, tzinfo=timezone.utc)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def proposal_context() -> ProposalContext:
    return ProposalContext(
        direction_hash=HASH_A,
        universe=("AAA", "BBB"),
        horizon="5d",
        decision_time=NOW,
        availability_assumptions=("daily close available after session end",),
        field_roles={
            "close": FieldRole.FEATURE,
            "volume": FieldRole.FEATURE,
            "future_return_5d": FieldRole.LABEL,
        },
    )


def frozen_inputs() -> FrozenIdentityInputs:
    return FrozenIdentityInputs(
        dataset_hash=HASH_A,
        universe_hash=HASH_B,
        split_hash=HASH_C,
        label_hash=HASH_D,
        cost_hash=canonical_content_hash({"cost": "visible-v1"}),
        code_hash=canonical_content_hash({"code": "evolution-v1"}),
        availability_hash=canonical_content_hash({"pit": "v1"}),
    )


class CountingProvider:
    def __init__(self, ledger: TrialLedger | None = None) -> None:
        self.delegate = DeterministicEvolutionProvider()
        self.ledger = ledger
        self.calls = 0
        self.contexts: list[str] = []
        self._lock = Lock()

    def propose(self, *, request):
        if self.ledger is not None:
            trial = self.ledger.replay().trials[f"trial-{request.child_id}"]
            assert trial.status is TrialStatus.STARTED
        with self._lock:
            self.calls += 1
            self.contexts.append(request.context.prompt_text())
        return self.delegate.propose(request=request)


class PrivilegedProvider:
    def propose(self, *, request):
        payload = dict(DeterministicEvolutionProvider().propose(request=request))
        payload["promotion"] = {"verdict": "PROMOTE_CHALLENGER"}
        return payload


class ExplodingProvider:
    def propose(self, *, request):
        raise RuntimeError("deterministic generation failure")


class RejectingGate:
    def __init__(self, outcome: GateOutcome) -> None:
        self.outcome = outcome

    def assess(self, *, proposal, request):
        code = (
            FailureCode.INVALID_TIME_ORDER
            if self.outcome is GateOutcome.PIT_REJECTION
            else FailureCode.INVALID_VALUE
        )
        return GateResult(outcome=self.outcome, failure_code=code)


def parent(
    candidate_id: str,
    candidate_hash: str,
    *,
    score: float = 1.0,
) -> VisibleParentEvidence:
    return VisibleParentEvidence(
        candidate_id=candidate_id,
        candidate_hash=candidate_hash,
        evidence_hash=canonical_content_hash({"evidence": candidate_id}),
        net_icir=score,
        stability=0.8,
        cost=0.001,
        novelty=0.7,
        incrementality=0.05,
        rank_ic_diagnostic=0.99,
    )


def build_controller(
    root,
    *,
    limits: EvolutionLimits | None = None,
    provider=None,
    gate=None,
):
    root.mkdir(parents=True, exist_ok=True)
    ledger = TrialLedger(root / "trial-ledger.sqlite3", clock=lambda: NOW)
    memory = ProposalMemory(LineageStore(root / "lineage-memory.sqlite3"))
    actual_provider = provider or CountingProvider(ledger)
    controller = EvolutionController(
        family_id="family-evolution",
        proposer_id="evolution-draft-provider",
        ledger=ledger,
        memory=memory,
        limits=limits or EvolutionLimits(),
        proposal_context=proposal_context(),
        identity_authority=GenerationIdentityAuthority(frozen_inputs()),
        seed=11,
        provider=actual_provider,
        gate=gate,
        clock=lambda: NOW,
    )
    return controller, ledger, memory, actual_provider


def test_original_mutation_and_crossover_are_draft_only_with_replayable_lineage(
    tmp_path,
) -> None:
    limits = EvolutionLimits(
        max_generations=3,
        max_children=6,
        family_budget=4,
        max_revisions=1,
    )
    controller, ledger, memory, provider = build_controller(
        tmp_path / "operators",
        limits=limits,
    )
    original = controller.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
    )
    original_child = original.children[0]
    mutation = controller.generate(
        operator=EvolutionOperator.MUTATION,
        generation=1,
        count=1,
        parent_evidence=(
            parent(original_child.child_id, original_child.proposal.artifact_hash),
        ),
    )
    mutation_child = mutation.children[0]
    crossover = controller.generate(
        operator=EvolutionOperator.CROSSOVER,
        generation=2,
        count=1,
        parent_evidence=(
            parent(original_child.child_id, original_child.proposal.artifact_hash, score=1.2),
            parent(mutation_child.child_id, mutation_child.proposal.artifact_hash, score=1.1),
        ),
    )

    assert all(
        child.proposal.state is ResearchState.DRAFT
        for child in (
            original_child,
            mutation_child,
            crossover.children[0],
        )
    )
    assert original_child.lineage.parent_ids == ()
    assert mutation_child.lineage.parent_ids == (original_child.child_id,)
    assert set(crossover.children[0].lineage.parent_ids) == {
        original_child.child_id,
        mutation_child.child_id,
    }
    assert all(
        child.lineage.budget_reservation_id.startswith("event-")
        and child.lineage.prompt_hash.startswith("sha256:")
        and 0 <= child.lineage.novelty <= 1
        for child in (original_child, mutation_child, crossover.children[0])
    )
    before = tuple(item.artifact_hash for item in memory.records())
    memory.clear()
    assert len(memory) == 0
    rebuilt = memory.rebuild()
    assert tuple(item.artifact_hash for item in rebuilt) == before
    assert ledger.replay().families["family-evolution"].consumed == 3
    assert all("sealed" not in item.lower() for item in provider.contexts)


def test_concurrent_children_reserve_before_generation_and_never_overspend(
    tmp_path,
) -> None:
    limits = EvolutionLimits(
        max_generations=1,
        max_children=8,
        family_budget=3,
        max_revisions=0,
    )
    root = tmp_path / "concurrent"
    root.mkdir()
    ledger = TrialLedger(root / "trial-ledger.sqlite3", clock=lambda: NOW)
    provider = CountingProvider(ledger)
    memory = ProposalMemory(LineageStore(root / "lineage-memory.sqlite3"))
    controller = EvolutionController(
        family_id="family-evolution",
        proposer_id="evolution-draft-provider",
        ledger=ledger,
        memory=memory,
        limits=limits,
        proposal_context=proposal_context(),
        identity_authority=GenerationIdentityAuthority(frozen_inputs()),
        seed=11,
        provider=provider,
        clock=lambda: NOW,
    )

    batch = controller.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=8,
        max_workers=8,
    )
    replay = ledger.replay()

    assert batch.committed_count == 3
    assert provider.calls == 3
    assert replay.families["family-evolution"].consumed == 3
    assert replay.families["family-evolution"].consumed <= limits.family_budget
    assert sum(
        child.failure_code is FailureCode.EVOLUTION_BUDGET_EXHAUSTED
        for child in batch.children
    ) == 5


@pytest.mark.parametrize(
    ("gate_outcome", "trial_outcome", "lineage_outcome"),
    [
        (GateOutcome.PIT_REJECTION, TrialOutcome.PIT_REJECTION, "PIT_FAILED"),
        (
            GateOutcome.EVALUATION_REJECTION,
            TrialOutcome.EVALUATION_REJECTION,
            "EVALUATION_FAILED",
        ),
    ],
)
def test_pit_and_evaluation_failures_consume_budget_and_commit_lineage(
    tmp_path,
    gate_outcome: GateOutcome,
    trial_outcome: TrialOutcome,
    lineage_outcome: str,
) -> None:
    controller, ledger, _, _ = build_controller(
        tmp_path / gate_outcome.value.lower(),
        limits=EvolutionLimits(
            max_generations=1,
            max_children=1,
            family_budget=1,
            max_revisions=0,
        ),
        gate=RejectingGate(gate_outcome),
    )

    child = controller.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
    ).children[0]
    replay = ledger.replay()
    trial = replay.trials[child.lineage.trial_id]

    assert child.proposal is None
    assert child.lineage.outcome == lineage_outcome
    assert replay.families["family-evolution"].consumed == 1
    assert trial.status is TrialStatus.TERMINAL
    assert trial.outcome is trial_outcome


def test_resume_does_not_regenerate_or_reconsume_a_committed_child(tmp_path) -> None:
    root = tmp_path / "resume"
    first, ledger, _, first_provider = build_controller(root)
    first_child = first.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
    ).children[0]
    assert first_provider.calls == 1

    second_provider = CountingProvider(ledger)
    second, _, second_memory, _ = build_controller(root, provider=second_provider)
    resumed = second.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
    ).children[0]

    assert resumed.resumed is True
    assert resumed.lineage.artifact_hash == first_child.lineage.artifact_hash
    assert resumed.proposal.artifact_hash == first_child.proposal.artifact_hash
    assert second_provider.calls == 0
    assert len(second_memory) == 1
    assert ledger.replay().families["family-evolution"].consumed == 1


def test_provider_cannot_write_promotion_and_failed_attempt_is_charged(tmp_path) -> None:
    controller, ledger, _, _ = build_controller(
        tmp_path / "privileged",
        limits=EvolutionLimits(
            max_generations=1,
            max_children=1,
            family_budget=1,
            max_revisions=0,
        ),
        provider=PrivilegedProvider(),
    )

    child = controller.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
    ).children[0]

    assert child.proposal is None
    assert child.failure_code is FailureCode.FORBIDDEN_PROPOSAL_PAYLOAD
    assert child.lineage.outcome == "PARSE_FAILED"
    assert not hasattr(child, "promotion")
    assert not hasattr(child, "verdict")
    assert ledger.replay().families["family-evolution"].consumed == 1


def test_generation_exception_is_terminal_and_consumes_family_budget(tmp_path) -> None:
    controller, ledger, _, _ = build_controller(
        tmp_path / "generation-failure",
        limits=EvolutionLimits(
            max_generations=1,
            max_children=1,
            family_budget=1,
            max_revisions=0,
        ),
        provider=ExplodingProvider(),
    )

    child = controller.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
    ).children[0]
    trial = ledger.replay().trials[child.lineage.trial_id]

    assert child.lineage.outcome == "GENERATION_FAILED"
    assert child.failure_code is FailureCode.ADAPTER_EXECUTION_FAILED
    assert trial.status is TrialStatus.TERMINAL
    assert trial.outcome is TrialOutcome.EXCEPTION
    assert ledger.replay().families["family-evolution"].consumed == 1


def test_cancelled_before_reservation_does_not_start_or_release_an_attempt(tmp_path) -> None:
    controller, ledger, _, provider = build_controller(
        tmp_path / "cancel",
        limits=EvolutionLimits(
            max_generations=1,
            max_children=2,
            family_budget=2,
            max_revisions=0,
        ),
    )
    token = CancellationToken()
    token.cancel()

    batch = controller.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=2,
        cancellation=token,
    )

    assert provider.calls == 0
    assert all(not child.started for child in batch.children)
    assert all(child.failure_code is FailureCode.EVOLUTION_CANCELLED for child in batch.children)
    replay = ledger.replay()
    assert replay.families["family-evolution"].consumed == 0
    assert replay.trials == {}


def test_same_seed_and_frozen_inputs_produce_identical_draft_and_lineage_hashes(
    tmp_path,
) -> None:
    first, _, _, _ = build_controller(tmp_path / "deterministic-a")
    second, _, _, _ = build_controller(tmp_path / "deterministic-b")

    left = first.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
    ).children[0]
    right = second.generate(
        operator=EvolutionOperator.ORIGINAL,
        generation=0,
        count=1,
    ).children[0]

    assert left.child_id == right.child_id
    assert left.proposal.artifact_hash == right.proposal.artifact_hash
    assert left.lineage.artifact_hash == right.lineage.artifact_hash


def test_limits_fail_closed_for_generation_child_and_revision_bounds(tmp_path) -> None:
    controller, _, _, _ = build_controller(
        tmp_path / "limits",
        limits=EvolutionLimits(
            max_generations=1,
            max_children=2,
            family_budget=2,
            max_revisions=0,
        ),
    )

    with pytest.raises(ContractValidationError):
        controller.generate(
            operator=EvolutionOperator.ORIGINAL,
            generation=1,
            count=1,
        )
    with pytest.raises(ContractValidationError):
        controller.generate(
            operator=EvolutionOperator.ORIGINAL,
            generation=0,
            count=3,
        )
    with pytest.raises(ContractValidationError):
        controller.generate(
            operator=EvolutionOperator.ORIGINAL,
            generation=0,
            count=1,
            revision=1,
        )
