"""Campaign-routed LLM runtime facade.

The facade keeps Agent D's public surface small while delegating actual branch
execution to the existing Research Campaign runtime.  It is intentionally a
wrapper: no Evidence, Promotion, Portfolio, Risk, Execution, broker, or journal
service is created here.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..contracts import FailureCode
from ..contracts.base import fail
from ..research_campaign.runtime import (
    CampaignRoutedLLMRuntime,
    CampaignRuntimeConfig,
    CampaignRuntimeManifest as BaseCampaignRuntimeManifest,
    CampaignStageExecutor,
    InMemoryCampaignArtifactRepository,
)
from .models import (
    ALLOWED_CAMPAIGN_RUNTIME_MODES,
    ALL_CAMPAIGN_DESKS,
    CAMPAIGN_LLM_RUNTIME_MODE,
    CAMPAIGN_LLM_RUNTIME_RULE_VERSION,
    CampaignDeskRunPlan,
    CampaignRuntimeCheckpoint,
    CampaignRuntimeFailureCode,
    CampaignRuntimeManifest,
    validate_branch_authority,
    validate_plan_bindings,
)


class CampaignRuntimeProtocol(Protocol):
    """Minimal protocol expected by integration callers."""

    def run(
        self,
        plan: CampaignDeskRunPlan | None = None,
        *,
        checkpoint: CampaignRuntimeCheckpoint | None = None,
        **kwargs: Any,
    ) -> CampaignRuntimeManifest: ...


class CampaignLLMRuntime:
    """Run only router-selected Campaign desks through a fixed protocol plan."""

    def __init__(
        self,
        *,
        runtime: CampaignRoutedLLMRuntime | None = None,
        config: CampaignRuntimeConfig | None = None,
        repository: InMemoryCampaignArtifactRepository | None = None,
        executor: CampaignStageExecutor | None = None,
        runtime_mode: str = CAMPAIGN_LLM_RUNTIME_MODE,
        provider_available: bool = False,
    ) -> None:
        self.runtime_mode = _provider_checked_mode(runtime_mode, provider_available=provider_available)
        self.repository = repository or InMemoryCampaignArtifactRepository()
        self._runtime = runtime or CampaignRoutedLLMRuntime(
            config=config,
            repository=self.repository,
            executor=executor,
        )

    def run(
        self,
        plan: CampaignDeskRunPlan | None = None,
        *,
        checkpoint: CampaignRuntimeCheckpoint | None = None,
        **kwargs: Any,
    ) -> CampaignRuntimeManifest:
        run_plan = _coerce_plan(plan, runtime_mode=self.runtime_mode, kwargs=kwargs)
        campaign = validate_plan_bindings(run_plan)
        checkpoint_state = checkpoint or CampaignRuntimeCheckpoint()
        base_manifest = self._runtime.run(
            run_id=run_plan.run_id,
            campaign=campaign,
            route=run_plan.route,
            protocol_plan=run_plan.protocol_plan,
            recovery=checkpoint_state.to_recovery_state(),
            control=checkpoint_state.to_control(),
        )
        validate_branch_authority(base_manifest.branch_results)
        return _wrap_manifest(
            run_plan=run_plan,
            checkpoint=checkpoint,
            base_manifest=base_manifest,
            runtime_mode=self.runtime_mode,
        )


def _coerce_plan(
    plan: CampaignDeskRunPlan | None,
    *,
    runtime_mode: str,
    kwargs: dict[str, Any],
) -> CampaignDeskRunPlan:
    if plan is not None and kwargs:
        fail(FailureCode.INVALID_VALUE, "pass either a CampaignDeskRunPlan or keyword runtime inputs, not both")
    if plan is not None:
        if plan.runtime_mode == "REAL_LLM" and runtime_mode != "REAL_LLM":
            fail(
                FailureCode.ADAPTER_UNAVAILABLE,
                "Campaign LLM runtime provider is unavailable; wrapper refuses to label the run REAL_LLM",
                campaign_runtime_failure_code=CampaignRuntimeFailureCode.PROVIDER_UNAVAILABLE.value,
            )
        return plan
    if "runtime_mode" not in kwargs:
        kwargs["runtime_mode"] = runtime_mode
    return CampaignDeskRunPlan(**kwargs)


def _wrap_manifest(
    *,
    run_plan: CampaignDeskRunPlan,
    checkpoint: CampaignRuntimeCheckpoint | None,
    base_manifest: BaseCampaignRuntimeManifest,
    runtime_mode: str,
) -> CampaignRuntimeManifest:
    selected = tuple(run_plan.route.selected_desks)
    not_selected = tuple(desk for desk in ALL_CAMPAIGN_DESKS if desk not in set(selected))
    return CampaignRuntimeManifest(
        run_id=base_manifest.run_id,
        campaign_hash=base_manifest.campaign_hash,
        route_hash=base_manifest.route_hash,
        protocol_plan_hash=base_manifest.protocol_plan_hash,
        runtime_version=CAMPAIGN_LLM_RUNTIME_RULE_VERSION,
        runtime_mode=runtime_mode,
        status=base_manifest.status,
        selected_desks=selected,
        not_selected_desks=not_selected,
        branch_results=base_manifest.branch_results,
        artifact_hashes=base_manifest.artifact_hashes,
        base_manifest_hash=base_manifest.artifact_hash,
        checkpoint_hash=checkpoint.artifact_hash if checkpoint is not None else None,
        awaiting_checkpoint_id=base_manifest.awaiting_checkpoint_id,
        cancelled=base_manifest.cancelled,
    )


def _provider_checked_mode(mode: str, *, provider_available: bool) -> str:
    if mode not in ALLOWED_CAMPAIGN_RUNTIME_MODES:
        fail(
            FailureCode.INVALID_VALUE,
            "runtime_mode must be a declared deterministic or REAL_LLM mode",
            runtime_mode=mode,
            allowed=tuple(sorted(ALLOWED_CAMPAIGN_RUNTIME_MODES)),
        )
    if mode == "REAL_LLM" and not provider_available:
        fail(
            FailureCode.ADAPTER_UNAVAILABLE,
            "Campaign LLM runtime provider is unavailable; wrapper refuses to label the run REAL_LLM",
            campaign_runtime_failure_code=CampaignRuntimeFailureCode.PROVIDER_UNAVAILABLE.value,
        )
    return mode


__all__ = [
    "CampaignLLMRuntime",
    "CampaignRuntimeProtocol",
]
