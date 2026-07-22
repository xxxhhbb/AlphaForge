"""U6 asynchronous control plane for confirmed Research OS V3 Campaigns.

The canonical :class:`ResearchRunManifest` is the only persisted job status.
This module schedules U4 DRAFT research; it owns no PIT, Evidence, Promotion,
portfolio, risk, execution, or ledger authority.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import fail
from ..llm_desks.safety import canonical_hash
from ..research_campaign import (
    CampaignRoute,
    CampaignState,
    DataAvailability,
    DeskCapabilityManifest,
    DeterministicCampaignRouter,
    RegisteredResearchContext,
    ResearchCampaign,
    default_desk_capabilities,
    require_campaign_executable,
)
from ..research_run_controller import (
    ResearchRunIdentityStore,
    ResearchRunLifecycle,
    ResearchRunManifest,
)
from ..run_recovery.api import RecoveryError
from ..specialist_research import (
    SpecialistFactorPreflight,
    SpecialistResearchProvider,
    build_preferred_specialist_provider,
)
from .multi_service import ResearchOSV3MultiSpecialistService
from .tools import FrozenResearchContext


ProviderFactory = Callable[[str], SpecialistResearchProvider]
CompletionHandler = Callable[[ResearchCampaign, CampaignRoute, FrozenResearchContext, object], None]
TerminalHandler = Callable[
    [ResearchCampaign, CampaignRoute, FrozenResearchContext, ResearchRunManifest],
    None,
]
_TERMINAL = frozenset(
    {
        ResearchRunLifecycle.COMPLETED,
        ResearchRunLifecycle.PARTIAL,
        ResearchRunLifecycle.BLOCKED,
        ResearchRunLifecycle.FAILED,
        ResearchRunLifecycle.CANCELLED,
    }
)


class ResearchOSV3CampaignControl:
    """Thread-safe local operator control over the U4 multi-specialist service.

    ``start`` and ``resume`` return after durable manifest creation and submit
    bounded work to a background executor.  Provider instances are constructed
    separately for every selected specialist.  Provider construction failure is
    recorded as ``ADAPTER_UNAVAILABLE`` and delegated to U4's typed branch result;
    credentials are never included in manifest fields or exception details.
    """

    def __init__(
        self,
        *,
        root: str | Path,
        provider_factory: ProviderFactory | None = None,
        dotenv_path: str | Path = "agent/.env",
        router: DeterministicCampaignRouter | None = None,
        max_background_jobs: int = 2,
        max_specialist_workers: int = 6,
        completion_handler: CompletionHandler | None = None,
        terminal_handler: TerminalHandler | None = None,
        preflight: SpecialistFactorPreflight | None = None,
    ) -> None:
        if not 1 <= max_background_jobs <= 8:
            fail(FailureCode.INVALID_VALUE, "max_background_jobs must be 1..8")
        self.root = Path(root).resolve()
        self.identity_store = ResearchRunIdentityStore(self.root)
        self.router = router or DeterministicCampaignRouter()
        if provider_factory is None:
            frozen_dotenv = Path(dotenv_path)
            provider_factory = lambda specialist_id: build_preferred_specialist_provider(specialist_id, frozen_dotenv)
        self.provider_factory = provider_factory
        self.completion_handler = completion_handler
        self.terminal_handler = terminal_handler
        self.multi_service = ResearchOSV3MultiSpecialistService(
            root=self.root,
            max_workers=max_specialist_workers,
            preflight=preflight,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_background_jobs, thread_name_prefix="alphaforge-v3-campaign"
        )
        self._lock = threading.RLock()
        self._futures: dict[str, Future[object]] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    def start(
        self,
        *,
        campaign: ResearchCampaign,
        context: FrozenResearchContext,
        run_id: str | None = None,
        registered_context: RegisteredResearchContext | None = None,
        desk_capabilities: tuple[DeskCapabilityManifest, ...] | None = None,
        seed: int = 7,
    ) -> ResearchRunManifest:
        """Durably create an idempotent job and schedule it in the background."""

        require_campaign_executable(campaign)
        if context.dataset_manifest_hash != campaign.data_manifest_hash:
            fail(FailureCode.INVALID_HASH, "Campaign and frozen context use different data manifests")
        route_result = self.router.route(
            campaign,
            data_availability=_data_availability(campaign, context),
            registered_context=registered_context or RegisteredResearchContext(),
            desk_capabilities=desk_capabilities or default_desk_capabilities(),
            routed_at=campaign.confirmed_at,
        )
        selected = route_result.route.selected_desks if route_result.route is not None else campaign.candidate_agents[:1]
        resolved_run_id = run_id or _stable_run_id(campaign, context, route_result.request_hash, seed)
        if route_result.state is CampaignState.BLOCKED or route_result.route is None:
            manifest = _blocked_manifest(
                run_id=resolved_run_id,
                campaign=campaign,
                context=context,
                route_plan_hash=route_result.request_hash,
                selected=selected,
                failures=(
                    route_result.failure_code.value
                    if route_result.failure_code is not None
                    else FailureCode.UPSTREAM_BLOCKED.value,
                ),
            )
            return self.identity_store.ensure_manifest(manifest)

        route = route_result.route
        with self._lock:
            existing = self.identity_store.load_manifest(resolved_run_id)
            if existing is not None and existing.current_stage in _TERMINAL:
                expected_static = (
                    existing.campaign_hash == campaign.artifact_hash
                    and existing.data_plan_hash == context.artifact_hash
                    and existing.route_plan_hash == route.artifact_hash
                    and existing.policy_hash == campaign.policy_hash
                    and existing.code_hash == campaign.code_hash
                    and existing.selected_group_ids == route.selected_desks
                    and existing.data_snapshot_hash == context.artifact_hash
                )
                if expected_static:
                    return existing
        providers, provider_failures = self._build_providers(route)
        proposed = _initial_manifest(
            run_id=resolved_run_id,
            campaign=campaign,
            context=context,
            route=route,
            providers=providers,
            provider_failures=provider_failures,
        )
        with self._lock:
            existing = self.identity_store.load_manifest(resolved_run_id)
            if existing is not None:
                # ensure_manifest is the identity/collision authority even when a
                # caller reuses a run_id with changed Campaign input.
                checked = self.identity_store.ensure_manifest(
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
                    ),
                    expected_artifact_hash=existing.artifact_hash,
                )
                active = self._futures.get(resolved_run_id)
                if checked.current_stage in _TERMINAL or (active is not None and not active.done()):
                    return checked
                manifest = checked
            else:
                manifest = self.identity_store.ensure_manifest(proposed)
            self._submit(
                manifest=manifest,
                campaign=campaign,
                route=route,
                context=context,
                providers=providers,
                seed=seed,
            )
            return self.identity_store.load_manifest(resolved_run_id) or manifest

    def status(self, run_id: str) -> ResearchRunManifest:
        """Read status exclusively from the canonical manifest."""

        manifest = self.identity_store.load_manifest(run_id)
        if manifest is None:
            raise KeyError(run_id)
        return manifest

    def job_active(self, run_id: str) -> bool:
        """Return whether the background completion path still owns the run.

        The multi-specialist scheduler commits its terminal branch manifest
        before the product completion handler computes Evidence and publishes
        the report.  Status readers use this signal to avoid racing that
        completion handler with a premature fallback report.
        """

        with self._lock:
            future = self._futures.get(run_id)
            return future is not None and not future.done()

    def cancel(self, run_id: str) -> ResearchRunManifest:
        """Request cooperative cancellation and persist it before returning.

        Python threads and in-flight provider HTTP calls are not force-killed.
        Their bounded execution may finish DRAFT branch commits, while the
        canonical run remains CANCELLED until an explicit ``resume``.
        """

        with self._lock:
            manifest = self.status(run_id)
            if manifest.current_stage in {
                ResearchRunLifecycle.COMPLETED,
                ResearchRunLifecycle.PARTIAL,
                ResearchRunLifecycle.BLOCKED,
                ResearchRunLifecycle.FAILED,
            }:
                return manifest
            event = self._cancel_events.setdefault(run_id, threading.Event())
            event.set()
            future = self._futures.get(run_id)
            if future is not None:
                future.cancel()
            cancelled = replace(
                manifest,
                current_stage=ResearchRunLifecycle.CANCELLED,
                typed_failures=tuple(sorted(set((*manifest.typed_failures, "CANCELLED")))),
                official_evidence_hash=None,
            )
            try:
                return self.identity_store.ensure_manifest(
                    cancelled,
                    expected_artifact_hash=manifest.artifact_hash,
                )
            except RecoveryError as exc:
                latest = self.status(run_id)
                if exc.code is FailureCode.ARTIFACT_COLLISION and latest.current_stage in _TERMINAL:
                    return latest
                raise

    def resume(
        self,
        *,
        campaign: ResearchCampaign,
        context: FrozenResearchContext,
        run_id: str,
        registered_context: RegisteredResearchContext | None = None,
        desk_capabilities: tuple[DeskCapabilityManifest, ...] | None = None,
        seed: int = 7,
    ) -> ResearchRunManifest:
        """Resume a cancelled/interrupted job without re-running committed branches."""

        with self._lock:
            existing = self.status(run_id)
            active = self._futures.get(run_id)
            if active is not None and not active.done():
                fail(FailureCode.INVALID_STATE, "run cancellation is still settling")
            if existing.current_stage is ResearchRunLifecycle.COMPLETED:
                return existing
            if existing.current_stage not in {
                ResearchRunLifecycle.CANCELLED,
                ResearchRunLifecycle.PLANNING,
                ResearchRunLifecycle.RESEARCHING,
            }:
                fail(FailureCode.INVALID_STATE, "only cancelled or interrupted jobs can resume")
            reset = replace(
                existing,
                current_stage=ResearchRunLifecycle.PLANNING,
                typed_failures=tuple(item for item in existing.typed_failures if item != "CANCELLED"),
                official_evidence_hash=None,
                report_hash=None,
            )
            self.identity_store.ensure_manifest(
                reset,
                expected_artifact_hash=existing.artifact_hash,
                allow_cancelled_resume=existing.current_stage is ResearchRunLifecycle.CANCELLED,
            )
            self._cancel_events.pop(run_id, None)
        return self.start(
            campaign=campaign,
            context=context,
            run_id=run_id,
            registered_context=registered_context,
            desk_capabilities=desk_capabilities,
            seed=seed,
        )

    def wait(self, run_id: str, *, timeout: float | None = None) -> ResearchRunManifest:
        """Testing/operator helper; the public API should normally poll ``status``."""

        with self._lock:
            future = self._futures.get(run_id)
        if future is not None:
            future.result(timeout=timeout)
        return self.status(run_id)

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _build_providers(
        self, route: CampaignRoute
    ) -> tuple[dict[str, SpecialistResearchProvider], tuple[str, ...]]:
        providers: dict[str, SpecialistResearchProvider] = {}
        failures: set[str] = set()
        for specialist_id in route.selected_desks:
            try:
                provider = self.provider_factory(specialist_id)
                if provider is None or not callable(getattr(provider, "next_turn", None)):
                    fail(FailureCode.ADAPTER_UNAVAILABLE, "specialist provider is unavailable")
                providers[specialist_id] = provider
            except Exception as exc:
                code = exc.code if isinstance(exc, ContractValidationError) else FailureCode.ADAPTER_UNAVAILABLE
                failures.add(code.value)
        return providers, tuple(sorted(failures))

    def _submit(self, *, manifest, campaign, route, context, providers, seed) -> None:
        event = threading.Event()
        self._cancel_events[manifest.run_id] = event
        self._futures[manifest.run_id] = self._executor.submit(
            self._run_job,
            manifest.run_id,
            campaign,
            route,
            context,
            providers,
            seed,
            event,
        )

    def _run_job(self, run_id, campaign, route, context, providers, seed, cancel_event) -> None:
        if cancel_event.is_set():
            return
        try:
            result = self.multi_service.run(
                run_id=run_id,
                campaign=campaign,
                route=route,
                context=context,
                providers=providers,
                seed=seed,
            )
            if self.completion_handler is not None and not cancel_event.is_set():
                self.completion_handler(campaign, route, context, result)
            if cancel_event.is_set():
                manifest = self.identity_store.load_manifest(run_id)
                if manifest is not None:
                    self.identity_store.ensure_manifest(
                        replace(
                            manifest,
                            current_stage=ResearchRunLifecycle.CANCELLED,
                            typed_failures=tuple(
                                sorted(set((*manifest.typed_failures, "CANCELLED")))
                            ),
                            official_evidence_hash=None,
                            report_hash=None,
                        ),
                        expected_artifact_hash=manifest.artifact_hash,
                    )
        except Exception as exc:
            manifest = self.identity_store.load_manifest(run_id)
            if manifest is None or manifest.current_stage is ResearchRunLifecycle.CANCELLED:
                return
            code = exc.code if isinstance(exc, ContractValidationError) else FailureCode.ADAPTER_EXECUTION_FAILED
            failed = replace(
                manifest,
                current_stage=ResearchRunLifecycle.FAILED,
                typed_failures=tuple(sorted(set((*manifest.typed_failures, code.value)))),
                report_hash=None,
            )
            try:
                failed = self.identity_store.ensure_manifest(
                    failed,
                    expected_artifact_hash=manifest.artifact_hash,
                )
                if self.terminal_handler is not None:
                    try:
                        self.terminal_handler(campaign, route, context, failed)
                    except Exception as report_exc:
                        latest = self.identity_store.load_manifest(run_id) or failed
                        report_code = (
                            report_exc.code.value
                            if isinstance(report_exc, ContractValidationError)
                            else "REPORT_FINALIZATION_FAILED"
                        )
                        self.identity_store.ensure_manifest(
                            replace(
                                latest,
                                typed_failures=tuple(
                                    sorted(set((*latest.typed_failures, report_code)))
                                ),
                            ),
                            expected_artifact_hash=latest.artifact_hash,
                        )
            except RecoveryError as update_exc:
                latest = self.identity_store.load_manifest(run_id)
                if update_exc.code is FailureCode.ARTIFACT_COLLISION and latest is not None and latest.current_stage is ResearchRunLifecycle.CANCELLED:
                    return
                raise


def _data_availability(campaign: ResearchCampaign, context: FrozenResearchContext) -> DataAvailability:
    scopes: set[str] = set()
    successful = {name for name, status in context.api_statuses.items() if status == "SUCCESS" and context.api_row_counts.get(name, 0) > 0}
    if successful & {"daily", "daily_basic", "adj_factor", "moneyflow"}:
        scopes.update(("price_volume", "liquidity"))
    if successful & {"fina_indicator", "income", "balancesheet", "cashflow", "daily_basic"}:
        scopes.add("fundamentals")
    if successful & {"forecast", "express"}:
        scopes.add("events")
    if successful & {"shibor", "index_daily"}:
        scopes.add("macro_regime")
    if context.factor_catalog_count:
        scopes.add("registered_factor_summary")
    return DataAvailability(
        available_data=tuple(sorted(scopes)),
        universe_ids=campaign.subject.universe,
        data_manifest_hash=context.dataset_manifest_hash,
        calendar_ids=(context.calendar_hash,),
    )


def _provider_identity(selected, providers: Mapping[str, SpecialistResearchProvider]) -> tuple[str, str]:
    active = [providers[item] for item in selected if item in providers]
    provider_ids = {getattr(item, "provider_id", type(item).__name__.lower()) for item in active}
    model_ids = {getattr(item, "model_id", "none") for item in active}
    return (
        next(iter(provider_ids)) if len(provider_ids) == 1 else "mixed_specialist_providers",
        next(iter(model_ids)) if len(model_ids) == 1 else "mixed_models",
    )


def _initial_manifest(*, run_id, campaign, context, route, providers, provider_failures) -> ResearchRunManifest:
    provider_id, model_id = _provider_identity(route.selected_desks, providers)
    return ResearchRunManifest(
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
        typed_failures=provider_failures,
        official_evidence_hash=None,
        report_hash=None,
    )


def _blocked_manifest(*, run_id, campaign, context, route_plan_hash, selected, failures) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id=run_id,
        campaign_hash=campaign.artifact_hash,
        intent_hash=canonical_hash({"question": campaign.question, "subject": campaign.subject.artifact_hash}),
        data_plan_hash=context.artifact_hash,
        route_plan_hash=route_plan_hash,
        policy_hash=campaign.policy_hash,
        code_hash=campaign.code_hash,
        selected_group_ids=tuple(selected),
        candidate_request_hashes=(),
        created_at=campaign.confirmed_at,
        runtime_version="research_os_v3_u6_control",
        provider_id="deterministic_campaign_router",
        model_id="none",
        data_snapshot_hash=context.artifact_hash,
        current_stage=ResearchRunLifecycle.BLOCKED,
        typed_failures=tuple(sorted(set(failures))),
    )


def _stable_run_id(campaign, context, route_request_hash, seed) -> str:
    digest = canonical_hash(
        {
            "campaign_hash": campaign.artifact_hash,
            "context_hash": context.artifact_hash,
            "route_request_hash": route_request_hash,
            "seed": seed,
            "runtime": "research_os_v3_u6_control",
        }
    ).split(":", 1)[1]
    return "run-" + digest[:20]


__all__ = ["CompletionHandler", "ProviderFactory", "ResearchOSV3CampaignControl"]
