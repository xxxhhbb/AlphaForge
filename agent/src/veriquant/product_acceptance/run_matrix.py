"""Execute a bounded shard of the frozen 24-scenario product matrix.

This is an operational product runner, not an evaluator.  It uses the same
confirmed Campaign and Research OS V3 path as the API, and records only
committed canonical artifacts for the mechanical acceptance assembler.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Mapping

from ..contracts import ContractValidationError
from ..data import canonical_content_hash
from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_os_v3.product_service import ResearchOSV3ProductService
from ..research_planner.scenario_matrix import PRODUCT_SCENARIOS, ProductScenario
from ..research_run_controller import ResearchRunLifecycle
from ..specialist_research import DeterministicSpecialistProvider


_TERMINAL = frozenset(
    {
        ResearchRunLifecycle.COMPLETED,
        ResearchRunLifecycle.PARTIAL,
        ResearchRunLifecycle.BLOCKED,
        ResearchRunLifecycle.FAILED,
        ResearchRunLifecycle.CANCELLED,
    }
)
_REGISTERED_BASELINE = "registered_momentum_20d"
_REGISTERED_CONTEXT_SCENARIOS = frozenset({"S19", "S20", "S21", "S22", "S24"})


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _confirmation_answers(plan: Mapping[str, object], scenario: ProductScenario) -> dict[str, str]:
    understanding = plan.get("researchUnderstanding")
    if not isinstance(understanding, Mapping):
        raise ValueError("plan researchUnderstanding is unavailable")
    unresolved = tuple(str(item) for item in understanding.get("unresolvedFields", ()))
    known = {
        "forecast_horizons": f"{scenario.expected_horizons[0] if scenario.expected_horizons else 5}d",
        "parent_factor_id": _REGISTERED_BASELINE,
        "portfolio_or_existing_factor_ids": f"factor_ids:{_REGISTERED_BASELINE}",
        "factor_id": _REGISTERED_BASELINE,
        "data_source_scope_time_semantics": (
            "source=approved_frozen_documents;fields=sentiment,entity_mapping;"
            "available_at=provider_publication_time"
        ),
        "source_document": "document_id:registered_quality_factor_definition_v1",
    }
    unexpected = tuple(sorted(set(unresolved) - set(known)))
    if unexpected:
        raise ValueError(f"unhandled clarification fields: {unexpected}")
    return {field: known[field] for field in unresolved}


def _plan_payload(scenario: ProductScenario) -> dict[str, object]:
    payload: dict[str, object] = {
        "question": scenario.question,
        "depth": "标准研究",
        "interactionStyle": "标准式",
    }
    if scenario.scenario_id in _REGISTERED_CONTEXT_SCENARIOS:
        payload["existingFactorIds"] = [_REGISTERED_BASELINE]
    return payload


def _record(service: ResearchOSV3ProductService, *, scenario_id: str, plan_id: str, run_id: str) -> dict[str, object]:
    manifest = service.status(run_id)
    result = service.load_result(run_id)
    selection = service.load_selection(run_id)
    report_path = service.report_root / run_id / "machine_report.json"
    receipt_path = service.report_root / run_id / "receipt.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    data = service.data_loader()
    if data is None:
        raise ValueError("frozen validation data disappeared after the run")
    return {
        "scenario_id": scenario_id,
        "plan_id": plan_id,
        "run_id": run_id,
        "terminal_stage": manifest.current_stage.value,
        "manifest_hash": manifest.artifact_hash,
        "result_hash": result.artifact_hash,
        "report_hash": str(receipt["report_hash"]),
        "report_payload_hash": canonical_content_hash(report),
        "selection_hash": selection.artifact_hash if selection is not None else None,
        "official_evidence_hash": manifest.official_evidence_hash,
        "candidate_count": result.candidate_count,
        "accepted_preflight_count": result.accepted_preflight_count,
        "trial_count": manifest.trial_count,
        "selected_specialists": list(manifest.selected_group_ids),
        "typed_failures": list(manifest.typed_failures),
        "provenance": {
            "source_label": data.source_label,
            "strict_provenance_available": data.strict_provenance_available,
            "dataset_artifact_hash": data.artifact_hash,
            "row_provenance_count": len(data.row_provenance),
        },
    }


def run_shard(
    *,
    data_root: Path,
    output_root: Path,
    first: int,
    last: int,
    seed: int,
    timeout: float,
    summary_name: str,
) -> int:
    if not 1 <= first <= last <= len(PRODUCT_SCENARIOS):
        raise ValueError("scenario range must be within 1..24")
    if (output_root / "plans").exists() or (output_root / "runtime").exists():
        raise ValueError("output root already contains a product run; use a fresh directory")
    repository = FrozenValidationDataRepository(data_root)
    data = repository.load(require_strict_provenance=True)
    if data is None:
        raise ValueError("strict frozen validation data is unavailable")
    if data.source_label != "APPROVED_PROXY_DATA_VENDOR":
        raise ValueError("24-scenario product run requires approved frozen real-data provenance")

    service = ResearchOSV3ProductService(
        root=output_root,
        data_loader=lambda: repository.load(require_strict_provenance=True),
        dotenv_path=output_root / "disabled-provider.env",
        provider_factory=lambda specialist_id: DeterministicSpecialistProvider(specialist_id),
    )
    records: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    try:
        for scenario in PRODUCT_SCENARIOS[first - 1 : last]:
            plan_id = ""
            run_id = ""
            try:
                plan = service.create_plan(_plan_payload(scenario))
                plan_id = str(plan["campaignId"])
                answers = _confirmation_answers(plan, scenario)
                confirmed = service.confirm_plan(plan_id, answers=answers or None)
                started = service.start(str(confirmed["campaignId"]), seed=seed)
                run_id = started.run_id
                terminal = service.control.wait(run_id, timeout=timeout)
                if terminal.current_stage not in _TERMINAL:
                    raise ValueError(f"run did not reach a terminal stage: {terminal.current_stage.value}")
                if terminal.report_hash is None:
                    raise ValueError("terminal run has no committed report")
                records.append(_record(service, scenario_id=scenario.scenario_id, plan_id=plan_id, run_id=run_id))
            except Exception as exc:
                code = exc.code.value if isinstance(exc, ContractValidationError) else type(exc).__name__
                failures.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "plan_id": plan_id,
                        "run_id": run_id,
                        "failure_code": code,
                        "detail": str(exc)[:240],
                    }
                )
    finally:
        service.control.close()

    payload = {
        "schema_version": "alphaforge.product-acceptance-shard/v1",
        "scenario_range": [first, last],
        "seed": seed,
        "source_label": data.source_label,
        "official_tushare": False,
        "dataset_artifact_hash": data.artifact_hash,
        "records": records,
        "failures": failures,
        "status": "PASSED" if len(records) == last - first + 1 and not failures else "FAILED",
    }
    _atomic_json(output_root / summary_name, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "PASSED" else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--first", type=int, required=True)
    parser.add_argument("--last", type=int, required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--summary-name", default="shard_summary.json")
    args = parser.parse_args(argv)
    return run_shard(
        data_root=args.data_root,
        output_root=args.output_root,
        first=args.first,
        last=args.last,
        seed=args.seed,
        timeout=args.timeout,
        summary_name=args.summary_name,
    )


if __name__ == "__main__":
    raise SystemExit(main())

