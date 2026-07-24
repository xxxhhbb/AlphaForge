"""Unified U6 product orchestration for Research OS V3.

This service owns product state and scheduling only.  Specialist models remain
DRAFT-only; all factor/PIT/metric work is delegated to ``U4OfficialEvidenceAdapter``
and the existing deterministic research evidence bridge.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from ..contracts import Component, FailureCode
from ..contracts.base import fail
from ..data import canonical_content_hash
from ..data_capability import DataRequirementPlanner, DatasetCapabilityAssessor
from ..data_capability.models import CapabilityStatus, DataGapCode, DatasetCapabilityReport, SCHEMA_VERSION
from ..dynamic_data_planning import (
    DataPlanningRequest,
    DynamicDataRequirementCompiler,
    UniverseKind,
    UniverseSpec,
)
from ..dynamic_data_integration import CampaignEvidenceDatasetBundle
from ..evidence.models import CostModel, WalkForwardSpec
from ..factor_library import build_factor_library_catalog
from ..research_campaign import (
    CampaignBudget,
    CampaignCompileRequest,
    InteractionStyle,
    ResearchCampaign,
    ResearchCampaignDraft,
    ResearchDepth,
    ResearchMode,
    UserCheckpoint,
    DeterministicCampaignCompiler,
)
from ..research_run_controller import ResearchRunIdentityStore, ResearchRunManifest
from ..research_run_controller import ResearchRunLifecycle
from ..research_validation_pipeline_v2 import FrozenValidationData
from ..specialist_research import SpecialistCheckpointStore, SpecialistFactorPreflight
from .control import ResearchOSV3CampaignControl
from .evidence_adapter import U4EvidenceAdapterRequest, U4EvidenceAdapterResult, U4OfficialEvidenceAdapter
from .multi_models import (
    MultiSpecialistBranchResult,
    MultiSpecialistBranchStatus,
    MultiSpecialistRunResult,
    MultiSpecialistRunStatus,
)
from .reporting import ResearchOSV3ReportBackend
from .registered_baselines import build_registered_baselines
from .selection import DeterministicVisibleCandidateSelector, V3CandidateSelectionResult
from .tools import APPROVED_SOURCE_LABEL, FrozenResearchContext


GROUP_LABELS = {
    "fundamental": "基本面研究组",
    "expectations_events": "预期与事件研究组",
    "market_structure": "市场行为与交易可行性研究组",
    "alternative_data": "另类数据研究组",
    "macro_regime": "宏观状态研究组",
    "meta_alpha": "组合增量研究组",
}


class ResearchOSV3ProductService:
    """Question -> confirmed Campaign -> specialists -> Evidence -> report."""

    def __init__(
        self,
        *,
        root: str | Path,
        data_loader: Callable[[], FrozenValidationData | None],
        dotenv_path: str | Path,
        provider_factory=None,
        campaign_data_repository=None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.data_loader = data_loader
        self.campaign_data_repository = campaign_data_repository
        self.compiler = DeterministicCampaignCompiler()
        self.run_root = self.root / "runtime"
        self.report_root = self.root / "reports"
        self.identity_store = ResearchRunIdentityStore(self.run_root)
        self.control = ResearchOSV3CampaignControl(
            root=self.run_root,
            dotenv_path=dotenv_path,
            provider_factory=provider_factory,
            completion_handler=self._complete,
            terminal_handler=self._finalize_terminal,
            preflight=SpecialistFactorPreflight(allow_legacy_factor_dag=False),
        )

    def create_plan(self, payload: Mapping[str, object]) -> dict[str, object]:
        question = str(payload.get("question") or "").strip()
        if not question:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "question is required")
        data, _records = self._plan_data(None)
        decision_time = data.snapshot.decision_time if data else datetime.now(timezone.utc)
        data_hash = data.artifact_hash if data else "sha256:" + "0" * 64
        universe = (data.snapshot.dataset_id,) if data else ("CN_ASHARE_APPROVED_PIT",)
        existing_factor_ids = _payload_identifiers(payload.get("existingFactorIds") or payload.get("existing_factor_ids"))
        portfolio_id = _payload_optional_identifier(payload.get("portfolioId") or payload.get("portfolio_id"))
        preliminary = self.compiler.intent_compiler.compile(
            question,
            decision_time=decision_time,
            universe=universe,
            existing_factor_ids=existing_factor_ids,
            portfolio_id=portfolio_id,
        )
        horizon = preliminary.forecast_horizons[0] if preliminary.forecast_horizons else 5
        result = self.compiler.compile(CampaignCompileRequest(
            user_text=question,
            decision_time=decision_time,
            universe=universe,
            default_horizon=f"{horizon}d",
            allowed_data=_allowed_data(data),
            data_manifest_hash=data_hash,
            policy_hash=canonical_content_hash({"policy": "research-os-v3-visible-v1"}),
            code_hash=canonical_content_hash({"runtime": "research-os-v3-u6"}),
            existing_factor_ids=existing_factor_ids,
            portfolio_id=portfolio_id,
        ))
        if result.draft is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "research question needs clarification")
        draft = _apply_user_mode(result.draft, payload)
        dynamic_plan, requirement, capability = _data_assessment(result.intent, data)
        self._write_contract(self._draft_path(draft.campaign_id), draft.to_dict())
        self._write_contract(self._plan_dir(draft.campaign_id) / "intent.json", result.intent.to_dict())
        self._write_contract(self._plan_dir(draft.campaign_id) / "dynamic_data_plan.json", dynamic_plan.to_dict())
        self._write_contract(self._plan_dir(draft.campaign_id) / "data_requirement.json", requirement.to_dict())
        self._write_contract(self._plan_dir(draft.campaign_id) / "data_capability.json", capability.to_dict())
        return self._plan_view(
            draft,
            data=data,
            intent=result.intent.to_dict(),
            requirement=requirement.to_dict(),
            capability=capability.to_dict(),
        )

    def confirm_plan(
        self,
        plan_id: str,
        *,
        answers: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        draft = ResearchCampaignDraft.from_payload(self._read_contract(self._draft_path(plan_id)))
        if answers is None and getattr(draft, "unresolved_fields", ()):
            answers = _default_resolution_answers(draft)
        if answers is not None:
            draft = draft.resolve_clarifications(answers, resolved_by=Component.OPERATOR)
            self._write_contract(self._draft_path(plan_id), draft.to_dict())
            self._write_contract(
                self._plan_dir(plan_id) / "clarification_resolution.json",
                {
                    "schema_version": "alphaforge.campaign-resolution/v1",
                    "campaign_id": plan_id,
                    "resolved_draft_hash": draft.artifact_hash,
                    "resolved_by": Component.OPERATOR.value,
                    "answers": dict(sorted(draft.resolution_answers.items())),
                },
            )
        if answers is None:
            draft = _apply_implicit_default_scope(draft)
            self._write_contract(self._draft_path(plan_id), draft.to_dict())
        data, _records = self._plan_data(plan_id)
        if data is None or data.artifact_hash != draft.data_manifest_hash:
            fail(FailureCode.INVALID_HASH, "confirmed plan requires the same frozen data identity")
        campaign = ResearchCampaign.confirm(draft, confirmed_at=draft.created_at, confirmed_by=Component.OPERATOR)
        self._write_contract(self._campaign_path(plan_id), campaign.to_dict())
        return self._plan_view(campaign, data=data, **self._data_details(plan_id))

    def bind_plan_data(
        self,
        plan_id: str,
        bundle: CampaignEvidenceDatasetBundle,
    ) -> dict[str, object]:
        """Bind verified query-specific data before operator confirmation."""

        if self.campaign_data_repository is None:
            fail(
                FailureCode.UNAUTHORIZED_CAPABILITY,
                "campaign data repository is not configured",
            )
        if self._campaign_path(plan_id).exists():
            fail(FailureCode.INVALID_STATE, "confirmed Campaign data cannot be replaced")
        if not isinstance(bundle, CampaignEvidenceDatasetBundle):
            fail(FailureCode.INVALID_VALUE, "campaign Evidence dataset bundle is invalid")
        draft = ResearchCampaignDraft.from_payload(
            self._read_contract(self._draft_path(plan_id))
        )
        self.campaign_data_repository.save(plan_id, bundle)
        data = bundle.frozen_data
        draft = replace(
            draft,
            subject=replace(
                draft.subject,
                universe=(data.snapshot.dataset_id,),
                allowed_data=_allowed_data(data),
                decision_time=data.snapshot.decision_time,
            ),
            data_manifest_hash=data.artifact_hash,
        )
        intent = self.compiler.intent_compiler.compile(
            draft.question,
            decision_time=data.snapshot.decision_time,
            universe=(data.snapshot.dataset_id,),
            existing_factor_ids=draft.subject.existing_factor_ids,
            portfolio_id=draft.subject.portfolio_id,
        )
        dynamic_plan, requirement, capability = _data_assessment(intent, data)
        self._write_contract(self._draft_path(plan_id), draft.to_dict())
        self._write_contract(
            self._plan_dir(plan_id) / "intent.json",
            intent.to_dict(),
        )
        self._write_contract(
            self._plan_dir(plan_id) / "dynamic_data_plan.json",
            dynamic_plan.to_dict(),
        )
        self._write_contract(
            self._plan_dir(plan_id) / "data_requirement.json",
            requirement.to_dict(),
        )
        self._write_contract(
            self._plan_dir(plan_id) / "data_capability.json",
            capability.to_dict(),
        )
        self._write_contract(
            self._plan_dir(plan_id) / "evidence_dataset_binding.json",
            {
                "schema_version": "alphaforge.plan-data-binding/v1",
                "campaign_id": plan_id,
                "bundle_hash": bundle.artifact_hash,
                "dataset_manifest_hash": bundle.manifest.artifact_hash,
                "data_manifest_hash": data.artifact_hash,
                "snapshot_hash": data.snapshot.artifact_hash,
                "projected_records_hash": bundle.manifest.projected_records_hash,
            },
        )
        return self._plan_view(
            draft,
            data=data,
            intent=intent.to_dict(),
            requirement=requirement.to_dict(),
            capability=capability.to_dict(),
        )

    def data_preparation_spec(self, plan_id: str):
        """Return deterministic, credential-free inputs for an external freezer."""

        draft = ResearchCampaignDraft.from_payload(
            self._read_contract(self._draft_path(plan_id))
        )
        base_data, _records = self._plan_data(None)
        decision_time = (
            base_data.snapshot.decision_time
            if base_data is not None
            else draft.subject.decision_time
        )
        intent = self.compiler.intent_compiler.compile(
            draft.question,
            decision_time=decision_time,
            universe=draft.subject.universe,
            existing_factor_ids=draft.subject.existing_factor_ids,
            portfolio_id=draft.subject.portfolio_id,
        )
        dynamic_plan, requirement = _compile_data_requirements(intent, base_data)
        return draft, intent, dynamic_plan, requirement

    def plan_view(self, plan_id: str) -> dict[str, object]:
        """Return the canonical product view from persisted plan contracts."""

        campaign_path = self._campaign_path(plan_id)
        if campaign_path.exists():
            value = ResearchCampaign.from_payload(
                self._read_contract(campaign_path),
                component=Component.OPERATOR,
            )
        else:
            value = ResearchCampaignDraft.from_payload(
                self._read_contract(self._draft_path(plan_id))
            )
        data, _records = self._plan_data(plan_id)
        return self._plan_view(value, data=data, **self._data_details(plan_id))

    def start(self, plan_id: str, *, seed: int = 7) -> ResearchRunManifest:
        campaign, data, context = self._inputs(plan_id)
        manifest = self.control.start(campaign=campaign, context=context, seed=seed)
        if _should_start_recovery_retry(manifest):
            retry = self._latest_or_next_retry(campaign, context, manifest, seed=seed)
            if retry is not None:
                manifest = retry
        self._write_contract(self._run_dir(manifest.run_id) / "campaign.json", campaign.to_dict())
        self._write_contract(self._run_dir(manifest.run_id) / "context.json", context.to_dict())
        if (
            manifest.current_stage
            in {
                ResearchRunLifecycle.COMPLETED,
                ResearchRunLifecycle.PARTIAL,
                ResearchRunLifecycle.BLOCKED,
                ResearchRunLifecycle.FAILED,
                ResearchRunLifecycle.CANCELLED,
            }
            and manifest.report_hash is None
            and not self.control.job_active(manifest.run_id)
        ):
            self._finalize_terminal(campaign, None, context, manifest)
            manifest = self.identity_store.load_manifest(manifest.run_id) or manifest
        return manifest

    def _latest_or_next_retry(
        self,
        campaign: ResearchCampaign,
        context: FrozenResearchContext,
        manifest: ResearchRunManifest,
        *,
        seed: int,
    ) -> ResearchRunManifest | None:
        base_run_id = manifest.run_id
        latest_retry: ResearchRunManifest | None = None
        for index in range(1, 100):
            candidate_id = f"{base_run_id}-retry{index}"
            candidate = self.identity_store.load_manifest(candidate_id)
            if candidate is None:
                if latest_retry is not None and not _should_start_recovery_retry(latest_retry):
                    return latest_retry
                return self.control.start(
                    campaign=campaign,
                    context=context,
                    run_id=candidate_id,
                    seed=seed,
                )
            latest_retry = candidate
            if (
                candidate.current_stage
                not in {
                    ResearchRunLifecycle.COMPLETED,
                    ResearchRunLifecycle.PARTIAL,
                    ResearchRunLifecycle.BLOCKED,
                    ResearchRunLifecycle.FAILED,
                    ResearchRunLifecycle.CANCELLED,
                }
                or not _should_start_recovery_retry(candidate)
            ):
                return candidate
        return latest_retry

    def status(self, run_id: str) -> ResearchRunManifest:
        manifest = self.control.status(run_id)
        if (
            manifest.current_stage
            in {
                ResearchRunLifecycle.COMPLETED,
                ResearchRunLifecycle.PARTIAL,
                ResearchRunLifecycle.BLOCKED,
                ResearchRunLifecycle.FAILED,
                ResearchRunLifecycle.CANCELLED,
            }
            and manifest.report_hash is None
            and not self.control.job_active(run_id)
        ):
            try:
                campaign = ResearchCampaign.from_payload(
                    self._read_contract(self._run_dir(run_id) / "campaign.json"),
                    component=Component.OPERATOR,
                )
                context = FrozenResearchContext.from_payload(
                    self._read_contract(self._run_dir(run_id) / "context.json")
                )
                self._finalize_terminal(campaign, None, context, manifest)
                manifest = self.control.status(run_id)
            except Exception:
                # Status remains fail-closed and truthful.  The caller still
                # sees the terminal state without a fabricated report.
                return manifest
        return manifest

    def cancel(self, run_id: str) -> ResearchRunManifest:
        manifest = self.control.cancel(run_id)
        if manifest.current_stage is ResearchRunLifecycle.CANCELLED:
            campaign = ResearchCampaign.from_payload(
                self._read_contract(self._run_dir(run_id) / "campaign.json"),
                component=Component.OPERATOR,
            )
            context = FrozenResearchContext.from_payload(
                self._read_contract(self._run_dir(run_id) / "context.json")
            )
            self._finalize_terminal(campaign, None, context, manifest)
            manifest = self.identity_store.load_manifest(run_id) or manifest
        return manifest

    def resume(self, run_id: str, plan_id: str, *, seed: int = 7) -> ResearchRunManifest:
        campaign, _data, context = self._inputs(plan_id)
        return self.control.resume(campaign=campaign, context=context, run_id=run_id, seed=seed)

    def load_result(self, run_id: str) -> MultiSpecialistRunResult:
        return MultiSpecialistRunResult.from_payload(self._read_contract(self._run_dir(run_id) / "result.json"))

    def load_evidence(self, run_id: str) -> U4EvidenceAdapterResult | None:
        path = self._run_dir(run_id) / "evidence.json"
        return U4EvidenceAdapterResult.from_payload(self._read_contract(path)) if path.exists() else None

    def load_selection(self, run_id: str) -> V3CandidateSelectionResult | None:
        path = self._run_dir(run_id) / "selection.json"
        return V3CandidateSelectionResult.from_payload(self._read_contract(path)) if path.exists() else None

    def _inputs(self, plan_id: str):
        campaign = ResearchCampaign.from_payload(self._read_contract(self._campaign_path(plan_id)), component=Component.OPERATOR)
        data, _records = self._plan_data(plan_id)
        if data is None or data.artifact_hash != campaign.data_manifest_hash:
            fail(FailureCode.INVALID_HASH, "Campaign frozen data is unavailable or changed")
        return campaign, data, _context_from_data(data)

    def _plan_data(self, plan_id: str | None):
        if plan_id is not None and self.campaign_data_repository is not None:
            bundle = self.campaign_data_repository.load(plan_id)
            if bundle is not None:
                return bundle.frozen_data, bundle.projected_records
        return self.data_loader(), ()

    def _complete(self, campaign, route, context, result: object) -> None:
        if not isinstance(result, MultiSpecialistRunResult):
            fail(FailureCode.INVALID_VALUE, "multi-specialist result is invalid")
        run_dir = self._run_dir(result.run_id)
        self._write_contract(run_dir / "result.json", result.to_dict())
        evidence = None
        selection = None
        data, _records = self._plan_data(campaign.campaign_id)
        if data is None or data.artifact_hash != campaign.data_manifest_hash:
            fail(FailureCode.INVALID_HASH, "frozen validation data changed before Evidence")
        evidence_authorized, evidence_gate = self._official_evidence_gate(campaign, data)
        self._write_contract(run_dir / "evidence_gate.json", evidence_gate)
        if (
            campaign.research_depth is not ResearchDepth.QUICK_EXPLORATION
            and result.candidate_refs
            and evidence_authorized
        ):
            outcomes = {}
            for branch in result.branches:
                outcome = SpecialistCheckpointStore(
                    self.run_root / "research_runs", run_id=result.run_id, specialist_id=branch.specialist_id
                ).load_outcome()
                if outcome is not None:
                    outcomes[branch.specialist_id] = outcome
            max_lookback = max(
                (candidate.lookback_sessions for outcome in outcomes.values() for candidate in outcome.candidates),
                default=1,
            )
            evidence = U4OfficialEvidenceAdapter(ledger_path=run_dir / "trial-ledger.sqlite3").run(
                U4EvidenceAdapterRequest(
                    run=result,
                    outcomes=outcomes,
                    frozen_data=data,
                    walk_forward=_walk_forward(data, warmup_sessions=max_lookback),
                    cost_model=CostModel(spread_bps=5.0, linear_impact_bps=10.0),
                    family_budget=campaign.budget.max_trials,
                    seed=7,
                    approver_id="evidence.court.visible",
                    registered_factors=build_registered_baselines(data, seed=7),
                    trusted_product_mode=True,
                )
            )
            self._write_contract(run_dir / "evidence.json", evidence.to_dict())
            family_ids = {
                candidate.artifact_hash: _selection_family_id(outcome.specialist_id, candidate.family_id)
                for outcome in outcomes.values()
                for candidate in outcome.candidates
            }
            selection = DeterministicVisibleCandidateSelector().select(
                evidence=evidence,
                family_ids=family_ids,
                family_budget_limit=campaign.budget.max_trials,
                formula_program_hashes={
                    ref.candidate_hash: ref.formula_program_hash
                    for ref in result.candidate_refs
                    if ref.formula_program_hash is not None
                },
            )
            self._write_contract(run_dir / "selection.json", selection.to_dict())
            manifest = self.identity_store.load_manifest(result.run_id)
            if manifest is None:
                fail(FailureCode.INVALID_HASH, "canonical manifest disappeared before report")
            self.identity_store.ensure_manifest(replace(
                manifest,
                trial_count=sum(item.budget_consumed for item in evidence.terminals),
                official_evidence_hash=evidence.official_evidence_hash,
            ))
        ResearchOSV3ReportBackend(run_root=self.run_root, publication_root=self.report_root).compose_and_publish(
            campaign=campaign,
            context=context,
            result=result,
            evidence=evidence,
            selection=selection,
        )

    def _official_evidence_gate(
        self,
        campaign: ResearchCampaign,
        data: FrozenValidationData,
    ) -> tuple[bool, dict[str, object]]:
        """Bind official Evidence authority to the confirmed plan's capability proof."""

        details = self._data_details(campaign.campaign_id)
        intent = details["intent"]
        requirement = details["requirement"]
        capability = details["capability"]
        reasons: list[str] = []
        support_level = _support_level(capability, intent)
        if support_level != "OFFICIAL_EVALUATION_READY":
            reasons.append("QUESTION_DATA_SEMANTICS_NOT_OFFICIAL_READY")
        if capability.get("status") != "READY":
            reasons.append("DATA_CAPABILITY_NOT_READY")
        if capability.get("snapshot_hash") != data.snapshot.artifact_hash:
            reasons.append("SNAPSHOT_IDENTITY_MISMATCH")
        if capability.get("requirement_plan_hash") != canonical_content_hash(requirement):
            reasons.append("REQUIREMENT_IDENTITY_MISMATCH")
        if data.artifact_hash != campaign.data_manifest_hash:
            reasons.append("CAMPAIGN_DATA_IDENTITY_MISMATCH")
        authorized = not reasons
        return authorized, {
            "schema_version": "alphaforge.official-evidence-gate/v1",
            "campaign_id": campaign.campaign_id,
            "campaign_hash": campaign.artifact_hash,
            "data_manifest_hash": data.artifact_hash,
            "snapshot_hash": data.snapshot.artifact_hash,
            "requirement_hash": canonical_content_hash(requirement),
            "capability_hash": canonical_content_hash(capability),
            "capability_status": capability.get("status", "UNAVAILABLE"),
            "question_data_support_level": support_level,
            "authorized": authorized,
            "authority": "DETERMINISTIC_DATA_CAPABILITY_GATE",
            "failure_codes": reasons,
        }

    def _finalize_terminal(
        self,
        campaign: ResearchCampaign,
        _route,
        context: FrozenResearchContext,
        manifest: ResearchRunManifest,
    ) -> None:
        """Publish a truthful report for every terminal lifecycle.

        Existing committed branch results are retained.  Missing branches are
        represented as zero-output terminal diagnostics; this method never
        creates candidates, Evidence, Promotion, or financial metrics.
        """

        if manifest.current_stage not in {
            ResearchRunLifecycle.COMPLETED,
            ResearchRunLifecycle.PARTIAL,
            ResearchRunLifecycle.BLOCKED,
            ResearchRunLifecycle.FAILED,
            ResearchRunLifecycle.CANCELLED,
        }:
            fail(FailureCode.INVALID_STATE, "terminal report requires a terminal manifest")
        result_path = self._run_dir(manifest.run_id) / "result.json"
        existing_result = (
            MultiSpecialistRunResult.from_payload(self._read_contract(result_path))
            if result_path.exists()
            else None
        )
        evidence = self.load_evidence(manifest.run_id) if (self._run_dir(manifest.run_id) / "evidence.json").exists() else None
        selection = self.load_selection(manifest.run_id) if (self._run_dir(manifest.run_id) / "selection.json").exists() else None
        committed = self._terminal_commits(manifest.run_id)
        existing_branches = {
            item.specialist_id: item
            for item in (existing_result.branches if existing_result is not None else ())
        }
        existing_branches.update(committed)
        fallback_status = {
            ResearchRunLifecycle.BLOCKED: MultiSpecialistBranchStatus.DATA_GAP,
            ResearchRunLifecycle.CANCELLED: MultiSpecialistBranchStatus.CANCELLED,
            ResearchRunLifecycle.PARTIAL: MultiSpecialistBranchStatus.PARTIAL_RESULT,
            ResearchRunLifecycle.FAILED: MultiSpecialistBranchStatus.FAILED,
            ResearchRunLifecycle.COMPLETED: MultiSpecialistBranchStatus.FAILED,
        }[manifest.current_stage]
        branches = []
        for specialist_id in sorted(manifest.selected_group_ids):
            branch = existing_branches.get(specialist_id)
            if branch is None:
                branch = MultiSpecialistBranchResult(
                    specialist_id=specialist_id,
                    branch_identity_hash=canonical_content_hash(
                        {
                            "run_id": manifest.run_id,
                            "specialist_id": specialist_id,
                            "campaign_hash": campaign.artifact_hash,
                            "context_hash": context.artifact_hash,
                            "terminal_stage": manifest.current_stage.value,
                        }
                    ),
                    status=fallback_status,
                    context_hash=context.artifact_hash,
                    outcome_hash=None,
                    candidate_refs=(),
                    typed_failures=manifest.typed_failures or (FailureCode.UPSTREAM_BLOCKED.value,),
                    missing_capabilities=("approved_frozen_research_inputs",)
                    if fallback_status is MultiSpecialistBranchStatus.DATA_GAP
                    else (),
                    provider_calls=0,
                )
            branches.append(branch)
        branches_tuple = tuple(branches)
        candidate_refs = tuple(
            sorted(
                (item for branch in branches_tuple for item in branch.candidate_refs),
                key=lambda item: (item.specialist_id, item.candidate_id, item.candidate_hash),
            )
        )
        run_status = {
            ResearchRunLifecycle.COMPLETED: MultiSpecialistRunStatus.COMPLETED,
            ResearchRunLifecycle.PARTIAL: MultiSpecialistRunStatus.PARTIAL,
            ResearchRunLifecycle.BLOCKED: MultiSpecialistRunStatus.BLOCKED,
            ResearchRunLifecycle.FAILED: MultiSpecialistRunStatus.FAILED,
            ResearchRunLifecycle.CANCELLED: MultiSpecialistRunStatus.CANCELLED,
        }[manifest.current_stage]
        result = MultiSpecialistRunResult(
            run_id=manifest.run_id,
            manifest_hash=manifest.artifact_hash,
            campaign_hash=campaign.artifact_hash,
            route_hash=manifest.route_plan_hash,
            context_hash=context.artifact_hash,
            status=run_status,
            branches=branches_tuple,
            candidate_refs=candidate_refs,
            committed_branch_hashes=tuple(item.artifact_hash for item in branches_tuple),
            candidate_count=len(candidate_refs),
            accepted_preflight_count=sum(item.preflight_status == "ACCEPTED" for item in candidate_refs),
            typed_failures=tuple(
                sorted(
                    set(
                        (*manifest.typed_failures, *(item for branch in branches_tuple for item in branch.typed_failures))
                    )
                )
            ),
        )
        if existing_result is not None and evidence is not None:
            # Evidence is cryptographically bound to the exact pre-finalization
            # result. Keep that result immutable and let the terminal manifest
            # determine the public failure/cancellation outcome.
            result = existing_result
        self._write_contract(result_path, result.to_dict())
        ResearchOSV3ReportBackend(
            run_root=self.run_root,
            publication_root=self.report_root,
        ).compose_and_publish(
            campaign=campaign,
            context=context,
            result=result,
            evidence=evidence,
            selection=selection,
        )

    def _terminal_commits(self, run_id: str) -> dict[str, MultiSpecialistBranchResult]:
        branches_root = self.identity_store.run_dir(run_id) / "branches"
        committed: dict[str, MultiSpecialistBranchResult] = {}
        if not branches_root.exists():
            return committed
        for path in sorted(branches_root.glob("*/commit.json")):
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                payload = envelope["payload"]
                if envelope.get("payload_hash") != canonical_content_hash(payload):
                    fail(FailureCode.INVALID_HASH, "terminal branch commit hash mismatch")
                branch = MultiSpecialistBranchResult.from_payload(payload)
            except Exception:
                fail(FailureCode.LEDGER_CORRUPTION, "terminal branch commit is corrupt")
            committed[branch.specialist_id] = branch
        return committed

    def _plan_view(self, value, *, data, intent=None, requirement=None, capability=None):
        intent = intent or {}
        requirement = requirement or {}
        capability = capability or {}
        support_level = _support_level(capability, intent)
        available = support_level in {"OFFICIAL_EVALUATION_READY", "PARTIAL_EVALUATION_READY", "RESEARCH_ONLY"}
        strict_capability_ready = capability.get("status") == "READY"
        sessions = len(data.snapshot.historical_universe) if data else 0
        symbols = len({row.symbol for row in data.snapshot.rows}) if data else 0
        return {
            "campaignId": value.campaign_id,
            "campaignHash": value.artifact_hash,
            "state": "已确认" if isinstance(value, ResearchCampaign) else "待确认",
            "question": value.question,
            "researchMode": value.mode.value,
            "researchSubject": value.subject.description,
            "researchUnderstanding": {
                "lookbackSessions": list(intent.get("factor_lookbacks", ())),
                "forecastHorizons": list(intent.get("forecast_horizons", ())),
                "targetVariable": intent.get("target_variable", "future_return"),
                "requiredData": list(intent.get("required_data", ())),
                "optionalData": list(intent.get("optional_data", ())),
                "clarificationQuestions": list(intent.get("clarification_questions", ())),
                "unresolvedFields": list(getattr(value, "unresolved_fields", ())),
                "resolutionAnswers": dict(getattr(value, "resolution_answers", {})),
            },
            "depth": {ResearchDepth.QUICK_EXPLORATION: "快速探索", ResearchDepth.STANDARD_RESEARCH: "标准研究", ResearchDepth.STRICT_VALIDATION: "严格验证"}[value.research_depth],
            "interactionStyle": {InteractionStyle.GUIDED: "引导式", InteractionStyle.STANDARD: "标准式", InteractionStyle.BATCH: "批处理式"}[value.interaction_style],
            "selectedSpecialists": [GROUP_LABELS[item] for item in value.candidate_agents],
            "plannedResearch": [item.value for item in value.method_models],
            "checkpoints": [item.description or item.stage for item in value.user_checkpoints],
            "stopConditions": [item.value for item in value.stop_conditions],
            "dataScope": {
                "sourceLabel": data.source_label if data else "UNAVAILABLE",
                "universe": ", ".join(value.subject.universe),
                "dateRange": f"{data.snapshot.start_time.date()} 至 {data.snapshot.decision_time.date()}" if data else "尚无冻结数据",
                "availability": (
                    "可正式评价"
                    if support_level == "OFFICIAL_EVALUATION_READY" and strict_capability_ready
                    else "可进行部分研究"
                    if available
                    else "需要补充数据"
                ),
                "supportLevel": support_level,
                "capabilityStatus": capability.get("status", "UNAVAILABLE"),
                "gapCodes": list(capability.get("gap_codes", ())),
                "fieldCoverage": {
                    item["field_name"]: item["coverage"]
                    for item in capability.get("field_proofs", ())
                },
                "minimumSessions": requirement.get("minimum_sessions"),
                "observedSessions": capability.get("observed_sessions", sessions),
                "limitations": (
                    []
                    if available and strict_capability_ready
                    else [
                        "当前冻结数据仍有覆盖率或双时间语义缺口；可完成的候选将逐项评价，其余返回明确的数据缺口。"
                    ]
                    if available
                    else ["当前冻结数据不足以完成所请求的正式评价；系统仍将返回研究或数据缺口报告。"]
                ),
            },
            "budget": {
                "specialistGroups": len(value.candidate_agents),
                "candidateLimit": value.budget.max_factor_families * 4,
                "trialLimit": value.budget.max_trials,
                "modelCallLimit": value.budget.max_llm_calls,
            },
            "dataSummary": {"sessions": sessions, "symbols": symbols},
        }

    def _draft_path(self, plan_id: str) -> Path:
        return self._plan_dir(plan_id) / "draft.json"

    def _campaign_path(self, plan_id: str) -> Path:
        return self._plan_dir(plan_id) / "campaign.json"

    def _plan_dir(self, plan_id: str) -> Path:
        return self.root / "plans" / _safe_id(plan_id)

    def _data_details(self, plan_id: str) -> dict[str, object]:
        root = self._plan_dir(plan_id)
        return {
            "intent": self._read_contract(root / "intent.json"),
            "requirement": self._read_contract(root / "data_requirement.json"),
            "capability": self._read_contract(root / "data_capability.json"),
        }

    def _run_dir(self, run_id: str) -> Path:
        return self.root / "products" / _safe_id(run_id)

    @staticmethod
    def _write_contract(path: Path, payload: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _read_contract(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            fail(FailureCode.INVALID_IDENTIFIER, "research plan or run does not exist")
        if not isinstance(payload, dict):
            fail(FailureCode.INVALID_HASH, "research artifact is not an object")
        return payload


def _safe_id(value: str) -> str:
    if not value or len(value) > 128 or any(token in value for token in ("..", "/", "\\", "\x00")):
        fail(FailureCode.INVALID_IDENTIFIER, "unsafe research identifier")
    return value


def _selection_family_id(specialist_id: str, family_name: str) -> str:
    digest = canonical_content_hash((specialist_id, family_name)).split(":", 1)[1][:12]
    return f"v3-{specialist_id}-{digest}"


def _should_start_recovery_retry(manifest: ResearchRunManifest) -> bool:
    if manifest.current_stage is not ResearchRunLifecycle.FAILED:
        return False
    recoverable = {
        FailureCode.UNAUTHORIZED_CAPABILITY.value,
        FailureCode.ADAPTER_EXECUTION_FAILED.value,
        "REPORT_FINALIZATION_FAILED",
        "INCOMPLETE_RESULT",
    }
    return bool(set(manifest.typed_failures) & recoverable)


def _data_assessment(intent, data: FrozenValidationData | None):
    dynamic_plan, requirement = _compile_data_requirements(intent, data)
    if data is None:
        capability = DatasetCapabilityReport(
            schema_version=SCHEMA_VERSION,
            requirement_plan_hash=requirement.artifact_hash,
            source_label=APPROVED_SOURCE_LABEL,
            snapshot_hash="sha256:" + "0" * 64,
            observed_start=None,
            observed_end=None,
            observed_sessions=0,
            observed_symbols=0,
            minimum_cross_section_observed=0,
            median_cross_section_observed=0.0,
            maximum_cross_section_observed=0,
            field_proofs=(),
            available_at_verified=False,
            revision_semantics_verified=False,
            source_provenance_verified=False,
            historical_universe_verified=False,
            label_rows=0,
            label_coverage=0.0,
            status=CapabilityStatus.UNAVAILABLE,
            gap_codes=(
                DataGapCode.FREEZE_MANIFEST_MISSING,
                DataGapCode.SNAPSHOT_NOT_FROZEN,
            ),
        )
        return dynamic_plan, requirement, capability
    capability = DatasetCapabilityAssessor().assess(
        requirement=requirement,
        frozen_data=data,
        bitemporal_records=getattr(data, "bitemporal_records", ()),
    )
    return dynamic_plan, requirement, capability


def _compile_data_requirements(intent, data: FrozenValidationData | None):
    lookback = max(intent.factor_lookbacks, default=20)
    horizon = intent.forecast_horizons[0] if intent.forecast_horizons else 5
    train, validation, sealed, buffer = _adaptive_split_budget(data, lookback=lookback, horizon=horizon)
    dynamic_plan = DynamicDataRequirementCompiler().compile(DataPlanningRequest(
        question=intent.question,
        research_mode=intent.mode.value,
        factor_lookback_sessions=lookback,
        label_horizon_sessions=horizon,
        train_sessions=train,
        validation_sessions=validation,
        sealed_sessions=sealed,
        buffer_sessions=buffer,
        start_date=data.snapshot.start_time.date() if data else None,
        end_date=data.snapshot.decision_time.date() if data else None,
        universe=_research_universe_spec(intent.question, intent.universe),
    ))
    requirement = DataRequirementPlanner().build(dynamic_plan)
    return dynamic_plan, requirement


def _research_universe_spec(question: str, universe_ids: tuple[str, ...]) -> UniverseSpec:
    """Translate product vocabulary into an honest acquisition universe.

    The approved production spine currently has a historical-index adapter.
    Generic A-share research therefore defaults to the explicit CSI300 PIT
    universe instead of claiming that a bounded index sample is all A-shares.
    An explicit all-market request remains all-market and will fail closed at
    preparation until an all-market spine adapter is available.
    """

    text = question.casefold().replace(" ", "")
    if any(token in text for token in ("全a", "全部a股", "全市场", "全体a股")):
        return UniverseSpec(kind=UniverseKind.HISTORICAL_ALL_A_SHARE)
    index_aliases = {
        "沪深300": "000300.SH",
        "中证500": "000905.SH",
        "中证1000": "000852.SH",
        "上证50": "000016.SH",
    }
    for alias, code in index_aliases.items():
        if alias in text:
            return UniverseSpec(
                kind=UniverseKind.HISTORICAL_INDEX_CONSTITUENTS,
                index_code=code,
            )
    explicit_codes = tuple(
        sorted({
            value.upper()
            for value in universe_ids
            if value.upper().endswith((".SH", ".SZ", ".BJ"))
        })
    )
    if explicit_codes:
        return UniverseSpec(kind=UniverseKind.EXPLICIT_SYMBOLS, symbols=explicit_codes)
    return UniverseSpec(
        kind=UniverseKind.HISTORICAL_INDEX_CONSTITUENTS,
        index_code="000300.SH",
    )


def _adaptive_split_budget(data: FrozenValidationData | None, *, lookback: int, horizon: int):
    if data is None:
        return 120, 60, 40, 20
    sessions = len(data.snapshot.historical_universe)
    buffer = max(2, min(10, horizon))
    embargo = max(1, (horizon + 1) // 2)
    distributable = sessions - lookback - 2 * horizon - embargo - buffer
    if distributable < 20:
        return 10, 5, 5, buffer
    train = max(10, int(distributable * 0.60))
    validation = max(5, int(distributable * 0.25))
    sealed = max(5, distributable - train - validation)
    return train, validation, sealed, buffer


def _support_level(capability: Mapping[str, object], intent: Mapping[str, object]) -> str:
    required = {
        str(item)
        for item in intent.get("required_data", ())
        if str(item) not in {"available_at", "published_at", "event_time", "trade_date", "ts_code"}
    }
    research_only = {"documents", "source_document", "definition", "entity_mapping"}
    registered = {"registered_evidence", "registered_factor_summary", "cost_history"}
    if required & research_only:
        return "RESEARCH_ONLY"
    if required & registered:
        return "DATA_ACQUISITION_REQUIRED"
    if capability.get("status") == "READY":
        return "OFFICIAL_EVALUATION_READY"
    if capability.get("status") == "PARTIAL":
        return "PARTIAL_EVALUATION_READY"
    return "DATA_ACQUISITION_REQUIRED"


def _payload_identifiers(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        fail(FailureCode.INVALID_VALUE, "existing_factor_ids must be an array")
    normalized = tuple(sorted({str(item).strip() for item in value if str(item).strip()}))
    if len(normalized) != len(tuple(item for item in value if str(item).strip())):
        fail(FailureCode.INVALID_VALUE, "existing_factor_ids must be unique")
    return normalized


def _payload_optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        fail(FailureCode.INVALID_VALUE, "portfolio_id cannot be empty")
    return normalized


def _allowed_data(data: FrozenValidationData | None) -> tuple[str, ...]:
    if data is None:
        return ("price_volume",)
    fields = set(data.snapshot.fields)
    coverage = data.snapshot.coverage_by_field
    scopes = ["price_volume"]
    if any(field in fields and float(coverage.get(field, 0.0)) >= 0.6 for field in {"roe", "roa", "revenue", "net_income"}):
        scopes.append("fundamentals")
    if any(field in fields and float(coverage.get(field, 0.0)) >= 0.6 for field in {"earnings_forecast", "earnings_surprise"}):
        scopes.append("events")
    if any(field in fields and float(coverage.get(field, 0.0)) >= 0.6 for field in {"shibor_overnight", "benchmark_return"}):
        scopes.append("macro_regime")
    return tuple(scopes)


def _apply_user_mode(draft: ResearchCampaignDraft, payload: Mapping[str, object]) -> ResearchCampaignDraft:
    depth = {"快速探索": ResearchDepth.QUICK_EXPLORATION, "标准研究": ResearchDepth.STANDARD_RESEARCH, "严格验证": ResearchDepth.STRICT_VALIDATION}.get(str(payload.get("depth") or "标准研究"))
    style = {"引导式": InteractionStyle.GUIDED, "标准式": InteractionStyle.STANDARD, "批处理式": InteractionStyle.BATCH}.get(str(payload.get("interactionStyle") or "标准式"))
    if depth is None or style is None:
        fail(FailureCode.INVALID_VALUE, "unsupported depth or interaction style")
    budget = {
        ResearchDepth.QUICK_EXPLORATION: CampaignBudget(max_factor_families=1, max_trials=1, max_llm_calls=4, max_runtime_seconds=60),
        ResearchDepth.STANDARD_RESEARCH: CampaignBudget(max_factor_families=9, max_trials=36, max_llm_calls=18, max_runtime_seconds=600),
        ResearchDepth.STRICT_VALIDATION: CampaignBudget(max_factor_families=18, max_trials=72, max_llm_calls=36, max_runtime_seconds=1200),
    }[depth]
    checkpoints = [UserCheckpoint(checkpoint_id="checkpoint.after_research_brief", stage="after_research_brief", description="确认研究计划")]
    if depth is not ResearchDepth.QUICK_EXPLORATION:
        checkpoints.append(UserCheckpoint(checkpoint_id="checkpoint.after_candidate_shortlist", stage="after_candidate_shortlist", description="确认候选短名单"))
    return replace(draft, research_depth=depth, interaction_style=style, budget=budget, user_checkpoints=tuple(checkpoints))


def _default_resolution_answers(draft: ResearchCampaignDraft) -> dict[str, str]:
    """Provide safe product defaults when the UI has no clarification form yet.

    These defaults only choose a visible research scope.  They do not create
    official evidence, promotion, portfolio, risk, order, or ledger authority.
    """

    answers: dict[str, str] = {}
    for field in getattr(draft, "unresolved_fields", ()):
        if field == "forecast_horizons":
            answers[field] = draft.subject.horizon
        elif field == "parent_factor_id":
            answers[field] = "registered_momentum_baseline"
        elif field == "factor_id":
            answers[field] = "registered_momentum_baseline"
        elif field == "portfolio_or_existing_factor_ids":
            answers[field] = "factor_ids:registered_momentum_baseline,registered_value_baseline"
        elif field == "universe":
            answers[field] = ",".join(draft.subject.universe)
    return answers


def _apply_implicit_default_scope(draft: ResearchCampaignDraft) -> ResearchCampaignDraft:
    existing = draft.subject.existing_factor_ids
    portfolio_id = draft.subject.portfolio_id
    if (
        draft.mode in {ResearchMode.PORTFOLIO_GAP_SEARCH, ResearchMode.DIAGNOSE_FACTOR_PORTFOLIO}
        and not (portfolio_id or existing)
    ):
        existing = ("registered_momentum_baseline", "registered_value_baseline")
    elif (
        draft.mode in {ResearchMode.IMPROVE_EXISTING_FACTOR, ResearchMode.MONITOR_DECAY_RETIREMENT}
        and not existing
    ):
        existing = ("registered_momentum_baseline",)
    if existing == draft.subject.existing_factor_ids:
        return draft
    return replace(
        draft,
        subject=replace(draft.subject, existing_factor_ids=existing),
        resolution_answers={
            **dict(draft.resolution_answers),
            "implicit_product_scope": ",".join(existing),
        },
        resolved_by=Component.OPERATOR,
    )


def _context_from_data(data: FrozenValidationData) -> FrozenResearchContext:
    fields = tuple(sorted(data.snapshot.fields))
    statuses = {
        "daily": _dataset_status(data, {"open", "high", "low", "close", "volume"}, minimum=0.8),
        "daily_basic": _dataset_status(data, {"market_cap", "turnover_rate", "volume"}, minimum=0.6),
        "fina_indicator": _dataset_status(data, {"roe", "roa", "gross_margin"}, minimum=0.6),
        "income": _dataset_status(data, {"revenue", "net_income"}, minimum=0.6),
        "balancesheet": _dataset_status(data, {"total_assets", "total_liabilities"}, minimum=0.6),
        "cashflow": _dataset_status(data, {"operating_cashflow"}, minimum=0.6),
        "forecast": _dataset_status(data, {"earnings_forecast"}, minimum=0.6),
        "express": _dataset_status(data, {"earnings_surprise"}, minimum=0.6),
        "shibor": _dataset_status(data, {item for item in fields if item.startswith("shibor")}, minimum=0.6),
        "index_daily": _dataset_status(data, {"benchmark_return"}, minimum=0.6),
    }
    row_count = len(data.snapshot.rows)
    counts = {key: row_count if value == "SUCCESS" else 0 for key, value in statuses.items()}
    catalog = build_factor_library_catalog()
    dates = sorted(data.snapshot.historical_universe)
    # These are deterministic, PIT-safe registered factor-score identities.
    # They are visible research references only, never official metrics or
    # Promotion evidence.  Their presence lets the combination specialist
    # investigate overlap against existing baselines during the first phase.
    visible_registered_factor_hashes = tuple(
        item.factor_hash for item in build_registered_baselines(data, seed=7)
    )
    return FrozenResearchContext(
        context_id="approved-proxy-v3-" + data.artifact_hash[7:19],
        source_label=APPROVED_SOURCE_LABEL if data.source_label == APPROVED_SOURCE_LABEL else data.source_label,
        official_tushare=False,
        decision_time=data.snapshot.decision_time,
        data_start=dates[0] if dates else data.snapshot.start_time.date().isoformat(),
        data_end=dates[-1] if dates else data.snapshot.decision_time.date().isoformat(),
        dataset_manifest_hash=data.artifact_hash,
        calendar_hash=canonical_content_hash(tuple(dates)),
        universe_hash=canonical_content_hash(data.snapshot.historical_universe),
        api_statuses=statuses,
        api_row_counts=counts,
        available_fields=fields,
        field_coverage=data.snapshot.coverage_by_field,
        factor_catalog_hash=catalog.catalog_hash,
        factor_catalog_count=catalog.total_records,
        visible_evidence_hashes=visible_registered_factor_hashes,
    )


def _dataset_status(data: FrozenValidationData, candidates: set[str], *, minimum: float) -> str:
    if not candidates:
        return "EMPTY"
    observed = [
        float(data.snapshot.coverage_by_field.get(field, 0.0))
        for field in candidates
        if field in data.snapshot.fields
    ]
    if not observed or max(observed) <= 0.0:
        return "EMPTY"
    return "SUCCESS" if max(observed) >= minimum else "PARTIAL"


def _walk_forward(data: FrozenValidationData, *, warmup_sessions: int = 0) -> WalkForwardSpec:
    periods = len(data.snapshot.historical_universe) - max(0, warmup_sessions)
    if periods < 3:
        fail(FailureCode.INVALID_VALUE, "at least three frozen periods are required for visible evaluation")
    train = max(1, periods // 2)
    validation = max(1, periods // 4)
    sealed = max(1, min(periods - train - validation, periods // 10 or 1))
    if train + validation + sealed > periods:
        train = periods - validation - sealed
    return WalkForwardSpec(train_periods=train, validation_periods=validation, sealed_periods=sealed, step_periods=max(1, validation))


__all__ = ["ResearchOSV3ProductService"]
