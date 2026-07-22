"""U4 Campaign-routed, failure-isolated multi-specialist V3 scheduler."""

from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash
from ..research_campaign import CampaignRoute, ResearchCampaign, ResearchDepth, RoutedDeskBudget
from ..research_run_controller import ResearchRunIdentityStore, ResearchRunLifecycle, ResearchRunManifest
from ..specialist_research import (
    BoundedResearchAgentLoop,
    PreflightStatus,
    SPECIALIST_PROFILES,
    SpecialistAgentOutcome,
    SpecialistCheckpointStore,
    SpecialistFactorPreflight,
    SpecialistResearchBudget,
    SpecialistResearchProvider,
    SpecialistResearchRequest,
    SpecialistResultKind,
    SpecialistStage,
    RestrictedResearchToolRegistry,
    get_specialist_profile,
)
from .multi_models import (
    MultiSpecialistBranchResult,
    MultiSpecialistBranchStatus,
    MultiSpecialistCandidateRef,
    MultiSpecialistRunResult,
    MultiSpecialistRunStatus,
)
from .tools import FrozenResearchContext, build_frozen_research_registry


class ResearchOSV3MultiSpecialistService:
    """One canonical run manifest; ThreadPool is scheduling only."""

    def __init__(
        self,
        *,
        root: str | Path,
        max_workers: int = 6,
        preflight: SpecialistFactorPreflight | None = None,
    ) -> None:
        if not 1 <= max_workers <= 6:
            fail(FailureCode.INVALID_VALUE, "max_workers must be 1..6")
        self.root = Path(root).resolve()
        self.identity_store = ResearchRunIdentityStore(self.root)
        self.max_workers = max_workers
        self.preflight = preflight

    def run(
        self,
        *,
        run_id: str,
        campaign: ResearchCampaign,
        route: CampaignRoute,
        context: FrozenResearchContext,
        providers: Mapping[str, SpecialistResearchProvider],
        seed: int = 7,
        cancel_requested: bool = False,
    ) -> MultiSpecialistRunResult:
        self._validate_inputs(campaign, route, context)
        budgets = {item.desk_id: item for item in route.desk_budgets}
        provider_id, model_id = _provider_identity(route.selected_desks, providers)
        proposed = ResearchRunManifest(
            run_id=run_id,
            campaign_hash=campaign.artifact_hash,
            intent_hash=canonical_hash({"question": campaign.question, "subject": campaign.subject.artifact_hash}),
            data_plan_hash=context.artifact_hash,
            route_plan_hash=route.artifact_hash,
            policy_hash=campaign.policy_hash,
            code_hash=campaign.code_hash,
            selected_group_ids=route.selected_desks,
            candidate_request_hashes=(),
            created_at=campaign.confirmed_at,
            runtime_version="research_os_v3_u4",
            provider_id=provider_id,
            model_id=model_id,
            data_snapshot_hash=context.artifact_hash,
            current_stage=ResearchRunLifecycle.PLANNING,
            remaining_iterations=sum(item.max_llm_calls for item in route.desk_budgets),
            remaining_tool_calls=8 * len(route.selected_desks),
            remaining_candidate_slots=sum(item.max_trials for item in route.desk_budgets),
        )
        existing = self.identity_store.load_manifest(run_id)
        if existing is not None:
            proposed = replace(
                proposed,
                current_stage=existing.current_stage,
                remaining_iterations=existing.remaining_iterations,
                remaining_tool_calls=existing.remaining_tool_calls,
                remaining_candidate_slots=existing.remaining_candidate_slots,
                candidate_count=existing.candidate_count,
                trial_count=existing.trial_count,
                typed_failures=existing.typed_failures,
                official_evidence_hash=existing.official_evidence_hash,
                report_hash=existing.report_hash,
            )
        manifest = self.identity_store.ensure_manifest(
            proposed,
            expected_artifact_hash=existing.artifact_hash if existing is not None else None,
        )
        committed = self._load_committed(run_id, route, context, budgets)
        if len(committed) == len(route.selected_desks):
            return self._finalize(manifest, route, context, committed)

        manifest = self.identity_store.ensure_manifest(
            replace(manifest, current_stage=ResearchRunLifecycle.RESEARCHING),
            expected_artifact_hash=manifest.artifact_hash,
        )
        pending = tuple(sorted(set(route.selected_desks) - set(committed)))
        immediate: dict[str, MultiSpecialistBranchResult] = {}
        runnable: list[str] = []
        for specialist_id in pending:
            identity = self._branch_identity(run_id, campaign, route, context, budgets[specialist_id], specialist_id)
            if cancel_requested:
                immediate[specialist_id] = self._not_started(specialist_id, identity, context, MultiSpecialistBranchStatus.CANCELLED, ("CANCELLED",), ())
            elif budgets[specialist_id].max_llm_calls < 3:
                immediate[specialist_id] = self._not_started(specialist_id, identity, context, MultiSpecialistBranchStatus.BUDGET_BLOCKED, ("BUDGET_BLOCKED",), ("minimum_llm_calls_3",))
            elif specialist_id not in providers:
                immediate[specialist_id] = self._not_started(specialist_id, identity, context, MultiSpecialistBranchStatus.FAILED, (FailureCode.ADAPTER_UNAVAILABLE.value,), ())
            else:
                runnable.append(specialist_id)
        for specialist_id, branch in immediate.items():
            self._commit(run_id, branch)
            committed[specialist_id] = branch

        if runnable:
            # Catalog discovery and whitelist construction are deterministic setup,
            # not branch work.  Build them serially so worker threads only consume
            # immutable, specialist-scoped registries.
            registries = {
                specialist_id: build_frozen_research_registry(
                    get_specialist_profile(specialist_id), context=context
                )
                for specialist_id in runnable
            }
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(runnable)), thread_name_prefix="alphaforge-v3-specialist") as executor:
                futures = {
                    executor.submit(
                        self._run_branch,
                        run_id=run_id,
                        campaign=campaign,
                        route=route,
                        context=context,
                        budget=budgets[specialist_id],
                        specialist_id=specialist_id,
                        provider=providers[specialist_id],
                        tools=registries[specialist_id],
                        seed=seed,
                    ): specialist_id
                    for specialist_id in runnable
                }
                for future in as_completed(futures):
                    specialist_id = futures[future]
                    try:
                        branch = future.result()
                    except Exception as exc:
                        identity = self._branch_identity(run_id, campaign, route, context, budgets[specialist_id], specialist_id)
                        failure_code = exc.code.value if isinstance(exc, ContractValidationError) else type(exc).__name__.upper()
                        branch = self._not_started(
                            specialist_id, identity, context, MultiSpecialistBranchStatus.FAILED,
                            (failure_code,), (), (_safe_failure_origin(exc),),
                        )
                    self._commit(run_id, branch)
                    committed[specialist_id] = branch
        return self._finalize(manifest, route, context, committed)

    @staticmethod
    def _validate_inputs(campaign: ResearchCampaign, route: CampaignRoute, context: FrozenResearchContext) -> None:
        if route.campaign_hash != campaign.artifact_hash:
            fail(FailureCode.INVALID_HASH, "route belongs to another Campaign")
        if not 1 <= len(route.selected_desks) <= 6 or len(set(route.selected_desks)) != len(route.selected_desks):
            fail(FailureCode.INVALID_VALUE, "route must select one to six unique specialists")
        if set(route.selected_desks) - set(SPECIALIST_PROFILES):
            fail(FailureCode.INVALID_IDENTIFIER, "route selected an unknown specialist")
        if campaign.data_manifest_hash != context.dataset_manifest_hash:
            fail(FailureCode.INVALID_HASH, "Campaign data manifest does not match frozen context")
        budget_ids = tuple(item.desk_id for item in route.desk_budgets)
        if set(budget_ids) != set(route.selected_desks) or len(budget_ids) != len(set(budget_ids)):
            fail(FailureCode.INVALID_STATE, "every selected specialist needs exactly one routed budget")
        if any(task.requires_official_evidence or task.allows_promotion for task in route.tasks):
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "U4 route cannot grant Evidence or Promotion authority")

    def _run_branch(
        self, *, run_id: str, campaign: ResearchCampaign, route: CampaignRoute,
        context: FrozenResearchContext, budget: RoutedDeskBudget, specialist_id: str,
        provider: SpecialistResearchProvider, tools: RestrictedResearchToolRegistry, seed: int,
    ) -> MultiSpecialistBranchResult:
        profile = get_specialist_profile(specialist_id)
        identity = self._branch_identity(run_id, campaign, route, context, budget, specialist_id)
        request = SpecialistResearchRequest(
            run_id=run_id,
            campaign_hash=campaign.artifact_hash,
            specialist_id=specialist_id,
            question=campaign.question,
            universe=_universe_id(campaign),
            label_horizon_sessions=_horizon_sessions(campaign.subject.horizon),
            decision_time=campaign.subject.decision_time,
            available_fields=context.available_fields,
            budget=SpecialistResearchBudget(
                max_iterations=budget.max_llm_calls,
                max_tool_calls=8,
                max_tokens=240_000,
                max_input_tokens=150_000,
                max_output_tokens=90_000,
                max_ideas=max(8, budget.max_factor_families * 4),
                max_candidate_families=budget.max_factor_families,
                max_combinations=max(8, budget.max_trials * 4),
                max_submitted_candidates=min(24, budget.max_trials),
                max_revisions=1,
                timeout_seconds=float(campaign.budget.max_runtime_seconds),
                family_trial_budget=budget.max_trials,
            ),
            research_depth=campaign.research_depth.value,
            requested_lookback_sessions=_requested_lookback(campaign.question),
            seed=seed,
        )
        store = SpecialistCheckpointStore(self.root / "research_runs", run_id=run_id, specialist_id=specialist_id)
        outcome = BoundedResearchAgentLoop(
            preflight=self.preflight,
            broad_candidate_search=True,
        ).run(
            request=request,
            profile=profile,
            provider=provider,
            tools=tools,
            checkpoint_store=store,
        )
        return _branch_from_outcome(identity, context, outcome, getattr(provider, "calls", outcome.usage.iterations))

    @staticmethod
    def _branch_identity(run_id, campaign, route, context, budget, specialist_id) -> str:
        return canonical_hash({
            "run_id": run_id, "campaign_hash": campaign.artifact_hash, "route_hash": route.artifact_hash,
            "context_hash": context.artifact_hash, "budget_hash": budget.artifact_hash, "specialist_id": specialist_id,
        })

    @staticmethod
    def _not_started(specialist_id, identity, context, status, failures, missing, origins=()) -> MultiSpecialistBranchResult:
        return MultiSpecialistBranchResult(
            specialist_id=specialist_id, branch_identity_hash=identity, status=status,
            context_hash=context.artifact_hash, outcome_hash=None, candidate_refs=(),
            typed_failures=tuple(sorted(set(failures))), missing_capabilities=tuple(sorted(set(missing))), provider_calls=0,
            failure_origins=tuple(sorted(set(origins))),
        )

    def _commit(self, run_id: str, branch: MultiSpecialistBranchResult) -> None:
        path = self._commit_path(run_id, branch.specialist_id)
        if path.exists():
            existing = self._read_commit(path)
            if existing.artifact_hash != branch.artifact_hash:
                fail(FailureCode.ARTIFACT_COLLISION, "branch commit already exists with different content")
            return
        _atomic_envelope(path, branch.to_dict())

    def _load_committed(self, run_id, route, context, budgets) -> dict[str, MultiSpecialistBranchResult]:
        committed = {}
        for specialist_id in route.selected_desks:
            path = self._commit_path(run_id, specialist_id)
            if not path.exists():
                continue
            branch = self._read_commit(path)
            if branch.context_hash != context.artifact_hash:
                fail(FailureCode.INVALID_HASH, "committed branch uses another frozen context")
            committed[specialist_id] = branch
        return committed

    def _commit_path(self, run_id: str, specialist_id: str) -> Path:
        return self.identity_store.run_dir(run_id) / "branches" / specialist_id / "commit.json"

    @staticmethod
    def _read_commit(path: Path) -> MultiSpecialistBranchResult:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            payload = envelope["payload"]
            if envelope["payload_hash"] != canonical_hash(payload):
                raise ValueError("hash mismatch")
            return MultiSpecialistBranchResult.from_payload(payload)
        except Exception as exc:
            fail(FailureCode.INVALID_HASH, "branch commit is corrupt", error=type(exc).__name__)

    def _finalize(self, manifest, route, context, committed) -> MultiSpecialistRunResult:
        if set(committed) != set(route.selected_desks):
            fail(FailureCode.INCOMPLETE_RESULT, "not every selected branch has a terminal commit")
        branches = tuple(committed[item] for item in sorted(committed))
        refs = tuple(sorted((ref for branch in branches for ref in branch.candidate_refs), key=lambda item: (item.specialist_id, item.candidate_id, item.candidate_hash)))
        status, lifecycle = _aggregate_status(branches)
        failures = tuple(sorted({code for branch in branches for code in branch.typed_failures}))
        outcomes = [self._load_outcome(manifest.run_id, item.specialist_id) for item in branches]
        if manifest.current_stage in {
            ResearchRunLifecycle.COMPLETED, ResearchRunLifecycle.PARTIAL, ResearchRunLifecycle.BLOCKED,
            ResearchRunLifecycle.FAILED, ResearchRunLifecycle.CANCELLED,
        }:
            final = manifest
        else:
            final = replace(
                manifest,
                current_stage=lifecycle,
                remaining_iterations=max(0, manifest.remaining_iterations - sum(item.usage.iterations for item in outcomes if item is not None)),
                remaining_tool_calls=max(0, manifest.remaining_tool_calls - sum(item.usage.tool_calls for item in outcomes if item is not None)),
                remaining_candidate_slots=max(0, manifest.remaining_candidate_slots - len(refs)),
                candidate_count=len(refs),
                typed_failures=tuple(sorted(set((*manifest.typed_failures, *failures)))),
                official_evidence_hash=None,
                report_hash=None,
            )
            final = self.identity_store.ensure_manifest(
                final,
                expected_artifact_hash=manifest.artifact_hash,
            )
        return MultiSpecialistRunResult(
            run_id=final.run_id, manifest_hash=final.artifact_hash, campaign_hash=final.campaign_hash,
            route_hash=route.artifact_hash, context_hash=context.artifact_hash, status=status,
            branches=branches, candidate_refs=refs,
            committed_branch_hashes=tuple(item.artifact_hash for item in branches),
            candidate_count=len(refs), accepted_preflight_count=sum(item.preflight_status == "ACCEPTED" for item in refs),
            typed_failures=failures,
        )

    def _load_outcome(self, run_id: str, specialist_id: str) -> SpecialistAgentOutcome | None:
        return SpecialistCheckpointStore(self.root / "research_runs", run_id=run_id, specialist_id=specialist_id).load_outcome()


def _branch_from_outcome(identity: str, context: FrozenResearchContext, outcome: SpecialistAgentOutcome, provider_calls: int) -> MultiSpecialistBranchResult:
    preflight = {item.candidate_id: item for item in outcome.preflight_results}
    refs = tuple(sorted((
        MultiSpecialistCandidateRef(
            specialist_id=outcome.specialist_id,
            candidate_id=item.candidate_id,
            candidate_hash=item.artifact_hash,
            preflight_status=preflight[item.candidate_id].status.value if item.candidate_id in preflight else "NOT_EVALUATED",
            factor_dag_hash=preflight[item.candidate_id].factor_dag_hash if item.candidate_id in preflight else None,
            failure_code=preflight[item.candidate_id].failure_code if item.candidate_id in preflight else "NOT_EVALUATED",
            formula_program_hash=preflight[item.candidate_id].formula_program_hash if item.candidate_id in preflight else None,
            formula_program_kind=preflight[item.candidate_id].formula_program_kind if item.candidate_id in preflight else "LEGACY_FACTOR_DAG",
            formula_manifest_hash=preflight[item.candidate_id].formula_manifest_hash if item.candidate_id in preflight else None,
        ) for item in outcome.candidates
    ), key=lambda item: (item.specialist_id, item.candidate_id, item.candidate_hash)))
    accepted = any(item.preflight_status == PreflightStatus.ACCEPTED.value for item in refs)
    if outcome.result_kind is SpecialistResultKind.EVALUATED_RESEARCH_RESULT and accepted:
        status = MultiSpecialistBranchStatus.COMPLETED_DRAFT
    elif outcome.result_kind is SpecialistResultKind.NEGATIVE_RESEARCH_RESULT:
        status = MultiSpecialistBranchStatus.NEGATIVE_RESULT
    elif outcome.result_kind in {
        SpecialistResultKind.DATA_OR_CAPABILITY_GAP_REPORT,
        SpecialistResultKind.PARTIAL_RESEARCH_RESULT,
    }:
        status = MultiSpecialistBranchStatus.PARTIAL_RESULT
    elif outcome.candidates and not accepted:
        status = MultiSpecialistBranchStatus.NEGATIVE_RESULT
    elif outcome.candidates:
        status = MultiSpecialistBranchStatus.PARTIAL_RESULT
    else:
        status = MultiSpecialistBranchStatus.FAILED
    failures = tuple(sorted({item for item in (outcome.failure_code, *(issue.failure_code for issue in outcome.draft_issues)) if item}))
    return MultiSpecialistBranchResult(
        specialist_id=outcome.specialist_id, branch_identity_hash=identity, status=status,
        context_hash=context.artifact_hash, outcome_hash=outcome.artifact_hash,
        candidate_refs=refs, typed_failures=failures, missing_capabilities=(), provider_calls=int(provider_calls),
    )


def _aggregate_status(branches):
    statuses = {item.status for item in branches}
    if statuses == {MultiSpecialistBranchStatus.CANCELLED}:
        return MultiSpecialistRunStatus.CANCELLED, ResearchRunLifecycle.CANCELLED
    complete = statuses & {
        MultiSpecialistBranchStatus.COMPLETED_DRAFT,
        MultiSpecialistBranchStatus.NEGATIVE_RESULT,
    }
    partial = statuses & {MultiSpecialistBranchStatus.PARTIAL_RESULT}
    non_complete = statuses - {
        MultiSpecialistBranchStatus.COMPLETED_DRAFT,
        MultiSpecialistBranchStatus.NEGATIVE_RESULT,
    }
    if complete and not non_complete:
        return MultiSpecialistRunStatus.COMPLETED, ResearchRunLifecycle.COMPLETED
    if statuses <= {MultiSpecialistBranchStatus.DATA_GAP, MultiSpecialistBranchStatus.BUDGET_BLOCKED}:
        return MultiSpecialistRunStatus.BLOCKED, ResearchRunLifecycle.BLOCKED
    # A committed partial research artifact is a legitimate research outcome,
    # not an infrastructure failure.  Preserve it even when another isolated
    # branch failed or was blocked; only an all-hard-failure run is FAILED.
    if complete or partial:
        return MultiSpecialistRunStatus.PARTIAL, ResearchRunLifecycle.PARTIAL
    return MultiSpecialistRunStatus.FAILED, ResearchRunLifecycle.FAILED


def _provider_identity(selected, providers):
    active = [providers[item] for item in selected if item in providers]
    provider_ids = {getattr(item, "provider_id", type(item).__name__.lower()) for item in active}
    model_ids = {getattr(item, "model_id", "none") for item in active}
    return (next(iter(provider_ids)) if len(provider_ids) == 1 else "mixed_specialist_providers", next(iter(model_ids)) if len(model_ids) == 1 else "mixed_models")


def _safe_failure_origin(exc: Exception) -> str:
    """Classify branch failures without persisting paths, provider text, or secrets."""

    message = str(exc).lower()
    if "artifact path escapes root" in message:
        return "CHECKPOINT_PATH_BOUNDARY"
    if "tool registry belongs" in message or "tool belongs to another" in message:
        return "TOOL_REGISTRY_OWNERSHIP"
    if "privileged field" in message or "future labels" in message:
        return "RESEARCH_FIELD_AUTHORITY"
    if "sealed" in message:
        return "SEALED_REFERENCE_REJECTED"
    if isinstance(exc, ContractValidationError):
        return "CONTRACT_" + exc.code.value
    return "RUNTIME_" + type(exc).__name__.upper()


def _horizon_sessions(value: str) -> int:
    match = re.search(r"(\d+)", value)
    if not match or int(match.group(1)) < 1:
        fail(FailureCode.INVALID_VALUE, "Campaign horizon must contain positive sessions")
    return int(match.group(1))


def _requested_lookback(question: str) -> int | None:
    match = re.search(r"(\d+)\s*(?:日|天|d|D)", question)
    return None if match is None else int(match.group(1))


def _universe_id(campaign: ResearchCampaign) -> str:
    if len(campaign.subject.universe) == 1:
        return campaign.subject.universe[0].lower()
    return "campaign_universe_" + canonical_hash(campaign.subject.universe).split(":", 1)[1][:12]


def _atomic_envelope(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {"payload": payload, "payload_hash": canonical_hash(payload)}
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(envelope, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    json.loads(temporary.read_text(encoding="utf-8"))
    os.replace(temporary, path)


__all__ = ["ResearchOSV3MultiSpecialistService"]
