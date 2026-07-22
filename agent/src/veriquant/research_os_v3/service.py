"""Single-mainline Research OS V3 service for bounded specialist research."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..contracts import FailureCode
from ..llm_desks.safety import canonical_hash
from ..research_run_controller import ResearchRunIdentityStore, ResearchRunLifecycle, ResearchRunManifest
from ..specialist_research import (
    BoundedResearchAgentLoop,
    PreflightStatus,
    RestrictedResearchToolRegistry,
    SpecialistAgentOutcome,
    SpecialistCheckpointStore,
    SpecialistResearchProvider,
    SpecialistResearchRequest,
    SpecialistResultKind,
    get_specialist_profile,
)
from .models import ResearchOSV3MigrationMap, ResearchOSV3RunView, ResearchResultKind


class ResearchOSV3Service:
    """Canonical V3 entry; owns orchestration state but no financial truth."""

    migration = ResearchOSV3MigrationMap()

    def __init__(
        self,
        *,
        root: str | Path,
        provider_id: str,
        model_id: str,
        data_snapshot_hash: str,
        policy_hash: str,
        code_hash: str,
        runtime_version: str = "research_os_v3",
    ) -> None:
        self.root = Path(root).resolve()
        self.identity_store = ResearchRunIdentityStore(self.root)
        self.provider_id = provider_id
        self.model_id = model_id
        self.data_snapshot_hash = data_snapshot_hash
        self.policy_hash = policy_hash
        self.code_hash = code_hash
        self.runtime_version = runtime_version

    def run_single_specialist(
        self,
        *,
        request: SpecialistResearchRequest,
        provider: SpecialistResearchProvider,
        tools: RestrictedResearchToolRegistry,
    ) -> ResearchOSV3RunView:
        profile = get_specialist_profile(request.specialist_id)
        proposed = self._initial_manifest(request)
        existing = self.identity_store.load_manifest(request.run_id)
        manifest = self.identity_store.ensure_manifest(proposed) if existing is None else self.identity_store.ensure_manifest(
            replace(
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
        )
        if manifest.current_stage not in {
            ResearchRunLifecycle.COMPLETED,
            ResearchRunLifecycle.PARTIAL,
            ResearchRunLifecycle.BLOCKED,
            ResearchRunLifecycle.FAILED,
            ResearchRunLifecycle.CANCELLED,
        }:
            manifest = replace(manifest, current_stage=ResearchRunLifecycle.RESEARCHING)
            self.identity_store.ensure_manifest(manifest)
        store = SpecialistCheckpointStore(self.root / "research_runs", run_id=request.run_id, specialist_id=request.specialist_id)
        outcome = BoundedResearchAgentLoop().run(
            request=request,
            profile=profile,
            provider=provider,
            tools=tools,
            checkpoint_store=store,
        )
        lifecycle, result_kind = self._classify(outcome, None)
        failures = tuple(sorted({item for item in (outcome.failure_code, *(issue.failure_code for issue in outcome.draft_issues)) if item}))
        manifest = replace(
            manifest,
            current_stage=lifecycle,
            remaining_iterations=max(0, request.budget.max_iterations - outcome.usage.iterations),
            remaining_tool_calls=max(0, request.budget.max_tool_calls - outcome.usage.tool_calls),
            remaining_candidate_slots=max(0, request.budget.max_submitted_candidates - len(outcome.candidates)),
            candidate_count=len(outcome.candidates),
            trial_count=0,
            typed_failures=failures,
            official_evidence_hash=None,
            report_hash=None,
        )
        self.identity_store.ensure_manifest(manifest)
        return self._view(manifest, outcome, result_kind)

    def api_status(self, run_id: str) -> ResearchOSV3RunView:
        return self._load_view(run_id)

    def workbench_status(self, run_id: str) -> ResearchOSV3RunView:
        return self._load_view(run_id)

    def report_status(self, run_id: str) -> ResearchOSV3RunView:
        return self._load_view(run_id)

    def _initial_manifest(self, request: SpecialistResearchRequest) -> ResearchRunManifest:
        return ResearchRunManifest(
            run_id=request.run_id,
            campaign_hash=request.campaign_hash,
            intent_hash=canonical_hash({"question": request.question, "horizon": request.label_horizon_sessions}),
            data_plan_hash=self.data_snapshot_hash,
            route_plan_hash=canonical_hash({"selected_specialists": (request.specialist_id,)}),
            policy_hash=self.policy_hash,
            code_hash=self.code_hash,
            selected_group_ids=(request.specialist_id,),
            candidate_request_hashes=(),
            created_at=request.decision_time,
            runtime_version=self.runtime_version,
            provider_id=self.provider_id,
            model_id=self.model_id,
            data_snapshot_hash=self.data_snapshot_hash,
            current_stage=ResearchRunLifecycle.PLANNING,
            remaining_iterations=request.budget.max_iterations,
            remaining_tool_calls=request.budget.max_tool_calls,
            remaining_candidate_slots=request.budget.max_submitted_candidates,
        )

    @staticmethod
    def _classify(outcome: SpecialistAgentOutcome, evidence_hash: str | None) -> tuple[ResearchRunLifecycle, ResearchResultKind]:
        if evidence_hash is not None:
            return ResearchRunLifecycle.COMPLETED, ResearchResultKind.EVALUATED_RESEARCH_RESULT
        if outcome.result_kind is SpecialistResultKind.DATA_OR_CAPABILITY_GAP_REPORT:
            return ResearchRunLifecycle.BLOCKED, ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT
        if outcome.result_kind is SpecialistResultKind.NEGATIVE_RESEARCH_RESULT:
            if outcome.capability_requests or (
                outcome.preflight_results
                and all(item.status is PreflightStatus.CAPABILITY_REQUEST for item in outcome.preflight_results)
            ):
                return ResearchRunLifecycle.BLOCKED, ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT
            return ResearchRunLifecycle.COMPLETED, ResearchResultKind.NEGATIVE_RESEARCH_RESULT
        if outcome.result_kind is SpecialistResultKind.SYSTEM_FAILED:
            if outcome.failure_code in {
                FailureCode.ADAPTER_UNAVAILABLE.value,
                FailureCode.QLIB_DATA_UNAVAILABLE.value,
                FailureCode.MISSING_REQUIRED_FIELD.value,
            }:
                return ResearchRunLifecycle.BLOCKED, ResearchResultKind.DATA_OR_CAPABILITY_GAP_REPORT
            return ResearchRunLifecycle.FAILED, ResearchResultKind.SYSTEM_FAILED
        if outcome.result_kind is SpecialistResultKind.PARTIAL_RESEARCH_RESULT:
            return ResearchRunLifecycle.PARTIAL, ResearchResultKind.PARTIAL_RESEARCH_RESULT
        if outcome.status.value == "COMPLETED":
            return ResearchRunLifecycle.PARTIAL, ResearchResultKind.PARTIAL_RESEARCH_RESULT
        return ResearchRunLifecycle.PARTIAL, ResearchResultKind.PARTIAL_RESEARCH_RESULT

    def _load_view(self, run_id: str) -> ResearchOSV3RunView:
        manifest = self.identity_store.load_manifest(run_id)
        if manifest is None:
            raise KeyError(run_id)
        specialist_id = manifest.selected_group_ids[0]
        store = SpecialistCheckpointStore(self.root / "research_runs", run_id=run_id, specialist_id=specialist_id)
        outcome = store.load_outcome()
        if outcome is None:
            return ResearchOSV3RunView(
                run_id=run_id,
                manifest_hash=manifest.artifact_hash,
                current_stage=manifest.current_stage.value,
                result_kind=ResearchResultKind.PARTIAL_RESEARCH_RESULT,
                selected_specialists=manifest.selected_group_ids,
                candidate_count=manifest.candidate_count,
                accepted_preflight_count=0,
                trial_count=manifest.trial_count,
                typed_failures=manifest.typed_failures,
                official_evidence_hash=manifest.official_evidence_hash,
                report_hash=manifest.report_hash,
                specialist_outcome_hash=None,
            )
        _, kind = self._classify(outcome, manifest.official_evidence_hash)
        return self._view(manifest, outcome, kind)

    @staticmethod
    def _view(manifest: ResearchRunManifest, outcome: SpecialistAgentOutcome, kind: ResearchResultKind) -> ResearchOSV3RunView:
        return ResearchOSV3RunView(
            run_id=manifest.run_id,
            manifest_hash=manifest.artifact_hash,
            current_stage=manifest.current_stage.value,
            result_kind=kind,
            selected_specialists=manifest.selected_group_ids,
            candidate_count=manifest.candidate_count,
            accepted_preflight_count=sum(item.status is PreflightStatus.ACCEPTED for item in outcome.preflight_results),
            trial_count=manifest.trial_count,
            typed_failures=manifest.typed_failures,
            official_evidence_hash=manifest.official_evidence_hash,
            report_hash=manifest.report_hash,
            specialist_outcome_hash=outcome.artifact_hash,
        )
