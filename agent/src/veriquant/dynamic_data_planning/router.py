"""Capability-aware selection of one to six research groups."""

from __future__ import annotations

from .catalog import research_group_catalog
from .models import (
    DataCapabilitySnapshot,
    DataReadiness,
    DatasetReadiness,
    DynamicDataPlan,
    ResearchGroupDecision,
    ResearchRoutingResult,
)


class CapabilityAwareResearchRouter:
    """Route only to groups backed by declared, non-secret data capability."""

    def route(
        self,
        plan: DynamicDataPlan,
        capability: DataCapabilitySnapshot | None = None,
    ) -> ResearchRoutingResult:
        if not isinstance(plan, DynamicDataPlan):
            raise ValueError("plan must be DynamicDataPlan")
        capability = capability or DataCapabilitySnapshot()
        if not isinstance(capability, DataCapabilitySnapshot):
            raise ValueError("capability must be DataCapabilitySnapshot")

        readiness_rows = tuple(
            _dataset_readiness(requirement, capability) for requirement in plan.requirements
        )
        by_dataset = {item.dataset_id: item for item in readiness_rows}
        decisions: list[ResearchGroupDecision] = []
        for group in research_group_catalog():
            theme_hits = tuple(sorted(set(group.themes).intersection(plan.themes), key=lambda x: x.value))
            relevant = bool(theme_hits)
            missing_datasets = tuple(
                dataset_id
                for dataset_id in group.core_datasets
                if dataset_id not in by_dataset
                or by_dataset[dataset_id].readiness is not DataReadiness.READY
            )
            missing_fields = tuple(
                sorted(
                    field
                    for field in group.useful_fields
                    if field not in capability.available_fields
                )
            )
            ready_core = len(group.core_datasets) - len(missing_datasets)
            theme_priority = (
                len(plan.themes)
                - min(plan.themes.index(theme) for theme in theme_hits)
                if theme_hits
                else 0
            )
            score = theme_priority * 100 + ready_core * 20 - len(missing_datasets) * 50
            eligible = relevant and not missing_datasets
            reason = _group_reason(relevant, eligible, missing_datasets, missing_fields)
            decisions.append(
                ResearchGroupDecision(
                    group_id=group.group_id,
                    selected=False,
                    score=score,
                    supported_themes=theme_hits,
                    missing_datasets=missing_datasets,
                    missing_fields=missing_fields,
                    reason=reason,
                )
            )

        ranked = sorted(
            (item for item in decisions if not item.missing_datasets and item.supported_themes),
            key=lambda item: (-item.score, item.group_id),
        )
        selected_ids = tuple(item.group_id for item in ranked[:6])
        final_decisions = tuple(
            ResearchGroupDecision(
                group_id=item.group_id,
                selected=item.group_id in selected_ids,
                score=item.score,
                supported_themes=item.supported_themes,
                missing_datasets=item.missing_datasets,
                missing_fields=item.missing_fields,
                reason=(
                    "selected: relevant mechanism and core datasets are declared available"
                    if item.group_id in selected_ids
                    else "excluded: six-group cap reached by higher-relevance capable groups"
                    if item.supported_themes and not item.missing_datasets
                    else item.reason
                ),
            )
            for item in sorted(decisions, key=lambda row: row.group_id)
        )

        ready = tuple(row.dataset_id for row in readiness_rows if row.readiness is DataReadiness.READY)
        partial = tuple(row.dataset_id for row in readiness_rows if row.readiness is DataReadiness.PARTIAL)
        unavailable = tuple(
            row.dataset_id for row in readiness_rows if row.readiness is DataReadiness.UNAVAILABLE
        )
        history_missing = capability.available_sessions < plan.history.minimum_total_sessions
        overall = (
            DataReadiness.UNAVAILABLE
            if not ready and not partial
            else DataReadiness.PARTIAL
            if partial or unavailable or history_missing
            else DataReadiness.READY
        )
        researchable_parts = tuple(
            [f"{dataset_id}: declared fields/APIs support bounded research" for dataset_id in ready]
            + [f"{dataset_id}: partial descriptive research only" for dataset_id in partial]
        )
        missing = list(unavailable)
        if history_missing:
            missing.append(
                f"history_sessions:{capability.available_sessions}/{plan.history.minimum_total_sessions}"
            )
        suggestions = _degradation_suggestions(ready, partial, unavailable, history_missing)
        return ResearchRoutingResult(
            plan_hash=plan.plan_hash,
            readiness=overall,
            dataset_readiness=readiness_rows,
            selected_group_ids=selected_ids,
            group_decisions=final_decisions,
            researchable_parts=researchable_parts,
            missing_requirements=tuple(missing),
            degradation_suggestions=suggestions,
            authority_restrictions=plan.authority_restrictions,
        )


def _dataset_readiness(requirement: object, capability: DataCapabilitySnapshot) -> DatasetReadiness:
    dataset_declared = requirement.dataset_id in capability.available_datasets
    api_set = set(capability.available_api_names)
    field_set = set(capability.available_fields)
    missing_apis = tuple(api for api in requirement.api_names if api not in api_set)
    missing_fields = tuple(field for field in requirement.required_fields if field not in field_set)
    # A frozen normalized dataset can be ready without exposing its upstream API
    # surface. Otherwise every requested upstream API must be declared.
    api_ready = dataset_declared or not missing_apis
    field_ready = not missing_fields
    if api_ready and field_ready:
        status = DataReadiness.READY
        reason = "required fields and dataset/API capability are available"
    elif dataset_declared or api_set.intersection(requirement.api_names) or field_set.intersection(requirement.required_fields):
        status = DataReadiness.PARTIAL
        reason = "some material exists, but missing semantics prohibit official evaluation"
    else:
        status = DataReadiness.UNAVAILABLE
        reason = "no declared capability for this dataset"
    return DatasetReadiness(
        dataset_id=requirement.dataset_id,
        readiness=status,
        missing_api_names=() if dataset_declared else missing_apis,
        missing_fields=missing_fields,
        reason=reason,
    )


def _group_reason(
    relevant: bool,
    eligible: bool,
    missing_datasets: tuple[str, ...],
    missing_fields: tuple[str, ...],
) -> str:
    if not relevant:
        return "excluded: group mechanism is not relevant to the compiled research themes"
    if missing_datasets:
        return "excluded: missing core datasets " + ", ".join(missing_datasets)
    if missing_fields:
        return "eligible with limited diagnostics: optional useful fields missing " + ", ".join(missing_fields)
    if eligible:
        return "eligible: relevant mechanism and core datasets are available"
    return "excluded"


def _degradation_suggestions(
    ready: tuple[str, ...],
    partial: tuple[str, ...],
    unavailable: tuple[str, ...],
    history_missing: bool,
) -> tuple[str, ...]:
    suggestions: list[str] = []
    if ready:
        suggestions.append("Proceed only with the listed researchable datasets and disclose omitted mechanisms.")
    if partial:
        suggestions.append("Use partial datasets for descriptive or sourced analysis, not official evidence.")
    if unavailable:
        suggestions.append(
            "Acquire and freeze missing datasets with provenance and PIT clocks before evaluating those hypotheses."
        )
    if history_missing:
        suggestions.append(
            "Acquire more history or explicitly reduce protocol budgets; never silently shorten the requested scope."
        )
    if not ready and not partial:
        suggestions.append(
            "Return a conceptual data-needs answer only; do not fabricate observations, metrics, or conclusions."
        )
    return tuple(suggestions)
