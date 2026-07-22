"""Compose verified market, financial and auxiliary receipts for scheduling only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ..dynamic_acquisition import stable_content_hash
from ..research_toolbox.contracts import canonical_hash


SCHEMA_VERSION = "alphaforge.research500-composite-coverage/v1"
_FINANCIAL_APIS = (
    "balancesheet", "cashflow", "express", "fina_indicator", "forecast", "income"
)
_CORE_APIS = (
    "adj_factor", "daily", "daily_basic", "index_daily", "moneyflow",
    "shibor", "stock_basic", "trade_cal",
)


def compose_coverage_inputs(
    *,
    base_inputs_path: str | Path,
    financial_freeze_path: str | Path,
    enrichment_receipt_path: str | Path,
    auxiliary_result_path: str | Path,
    registered_receipt_path: str | Path | None = None,
    representative_plan_path: str | Path | None = None,
    output_path: str | Path,
) -> Mapping[str, Any]:
    base = _mapping(base_inputs_path)
    financial = _mapping(financial_freeze_path)
    claimed_freeze_hash = financial.pop("artifact_hash", None)
    if claimed_freeze_hash != stable_content_hash(financial):
        raise ValueError("financial freeze manifest hash mismatch")
    enrichment = _mapping(enrichment_receipt_path)
    claimed_enrichment_hash = enrichment.pop("artifact_hash", None)
    if claimed_enrichment_hash != canonical_hash(enrichment) or enrichment.get("status") != "PASSED":
        raise ValueError("financial enrichment receipt is invalid")
    auxiliary = _mapping(auxiliary_result_path)
    if auxiliary.get("status") != "IMPLEMENTED_AND_TESTED":
        raise ValueError("auxiliary receipt set is not tested")
    security = auxiliary.get("security") or {}
    if security.get("secret_pattern_hits") != 0 or security.get("official_evidence_eligible") is not False:
        raise ValueError("auxiliary security boundary is invalid")

    registered_receipts = _registered_receipt_map(
        base.get("registered_context_receipts", [])
    )
    registered_component_hash = None
    evidence_index_hash = None
    if registered_receipt_path is not None:
        registered = _mapping(registered_receipt_path)
        registered_component_hash = registered.pop("artifact_hash", None)
        if registered_component_hash != canonical_hash(registered) or registered.get("status") != "PASSED":
            raise ValueError("registered baseline receipt is invalid")
        registered_receipts["registered_factor_summary"] = {
            "data_id": "registered_factor_summary",
            "status": "READY",
            "content_hash": registered_component_hash,
        }
        if representative_plan_path is not None:
            plan = _mapping(representative_plan_path)
            claimed_plan_hash = plan.pop("artifact_hash", None)
            if claimed_plan_hash != canonical_hash(plan):
                raise ValueError("representative plan hash mismatch")
            evidence_hashes = tuple(sorted(
                item["official_evidence_hash"]
                for item in plan.get("rows", ())
                if item.get("status") == "PASSED" and item.get("official_evidence_hash")
            ))
            if evidence_hashes:
                evidence_index_hash = canonical_hash(evidence_hashes)
                registered_receipts["registered_evidence"] = {
                    "data_id": "registered_evidence",
                    "status": "READY",
                    "content_hash": evidence_index_hash,
                }

    gateway = dict(base.get("gateway_manifest") or {})
    receipts = {
        str(item.get("source_id")): dict(item)
        for item in gateway.get("receipts", ())
        if item.get("source_id")
    }
    row_counts = financial.get("api_row_counts") or {}
    api_hashes = financial.get("api_manifest_hashes") or {}
    statuses = financial.get("api_statuses") or {}
    for api in _FINANCIAL_APIS:
        status = str(statuses.get(api, "MISSING"))
        rows = int(row_counts.get(api, 0))
        receipts[api] = {
            "source_id": api,
            "status": "SUCCESS" if status == "SUCCESS" and rows > 0 else "EMPTY",
            "authority": "OFFICIAL_INPUT",
            "row_count": rows,
            "content_hash": str(api_hashes.get(api) or stable_content_hash([])),
        }
    core_ready = all(
        receipts.get(api, {}).get("status") == "SUCCESS"
        and int(receipts[api].get("row_count", 0)) > 0
        for api in _CORE_APIS
    )
    if not core_ready:
        raise ValueError("base market gateway is not ready")
    gateway.pop("artifact_hash", None)
    gateway.update({
        "status": "READY",
        "receipts": [receipts[key] for key in sorted(receipts)],
        "market_component_hash": str((base.get("gateway_manifest") or {}).get("artifact_hash")),
        "financial_component_hash": str(claimed_freeze_hash),
        "enriched_dataset_hash": str(enrichment["bundle_hash"]),
    })
    gateway["artifact_hash"] = canonical_hash(gateway)

    auxiliary_receipts = []
    auxiliary_by_id: dict[str, dict[str, Any]] = {}
    for source_id, item in sorted((auxiliary.get("receipts") or {}).items()):
        receipt = {
            "source_id": source_id,
            "status": item["status"],
            "authority": item["authority"],
            "row_count": int(item.get("row_count", 0)),
            "content_hash": item["content_hash"],
        }
        auxiliary_by_id[source_id] = receipt
    auxiliary_by_id.update(
        _derived_auxiliary_receipts(
            gateway_receipts=receipts,
            auxiliary_receipts=auxiliary_by_id,
        )
    )
    auxiliary_receipts.extend(
        auxiliary_by_id[key] for key in sorted(auxiliary_by_id)
    )
    value = {
        "schema_version": SCHEMA_VERSION,
        "capability_report": base.get("capability_report"),
        "gateway_manifest": gateway,
        "auxiliary_receipts": auxiliary_receipts,
        "registered_context_receipts": [
            registered_receipts[key] for key in sorted(registered_receipts)
        ],
        "external_approval_needs": base.get("external_approval_needs", []),
        "component_hashes": {
            "financial_freeze": claimed_freeze_hash,
            "financial_enrichment": claimed_enrichment_hash,
            "auxiliary_probe": canonical_hash(auxiliary),
            "registered_baselines": registered_component_hash,
            "registered_evidence_index": evidence_index_hash,
        },
    }
    value["artifact_hash"] = canonical_hash(value)
    _atomic_json(Path(output_path).resolve(), value)
    return value


def _derived_auxiliary_receipts(
    *,
    gateway_receipts: Mapping[str, Mapping[str, Any]],
    auxiliary_receipts: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Create disclosed research-only composites from verified component receipts."""

    output: dict[str, dict[str, Any]] = {}
    document_components = [
        auxiliary_receipts[source_id]
        for source_id in ("research_reports", "stock_news")
        if _receipt_ready(auxiliary_receipts.get(source_id), auxiliary=True)
    ]
    if document_components:
        output["documents"] = _composite_receipt(
            source_id="documents",
            components=document_components,
            policy="alphaforge.documents-from-frozen-news-and-reports/v1",
        )

    forecast = gateway_receipts.get("forecast")
    express = gateway_receipts.get("express")
    if (
        not _receipt_ready(forecast, auxiliary=False)
        and _receipt_ready(express, auxiliary=False)
        and document_components
    ):
        proxy = _composite_receipt(
            source_id="forecast_proxy_express_documents",
            components=[express, *document_components],
            policy="alphaforge.forecast-proxy-express-documents/v1",
        )
        proxy["proxy_for"] = "forecast"
        proxy["limitation"] = "RESEARCH_ONLY_NOT_ACTUAL_MANAGEMENT_FORECAST"
        output[proxy["source_id"]] = proxy
    return output


def _receipt_ready(value: Mapping[str, Any] | None, *, auxiliary: bool) -> bool:
    authority = "AUXILIARY_RESEARCH_EVIDENCE" if auxiliary else "OFFICIAL_INPUT"
    return bool(
        value
        and value.get("status") in {"READY", "SUCCESS"}
        and value.get("authority") == authority
        and int(value.get("row_count", 0)) > 0
        and str(value.get("content_hash", "")).startswith("sha256:")
    )


def _composite_receipt(
    *,
    source_id: str,
    components: list[Mapping[str, Any]],
    policy: str,
) -> dict[str, Any]:
    component_hashes = tuple(sorted(str(item["content_hash"]) for item in components))
    return {
        "source_id": source_id,
        "status": "SUCCESS",
        "authority": "AUXILIARY_RESEARCH_EVIDENCE",
        "row_count": sum(int(item.get("row_count", 0)) for item in components),
        "content_hash": canonical_hash(
            {"policy": policy, "component_hashes": component_hashes}
        ),
        "component_hashes": list(component_hashes),
        "proxy_policy": policy,
    }


def _registered_receipt_map(values: Any) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for raw in values or ():
        if not isinstance(raw, Mapping):
            raise ValueError("registered context receipt must be an object")
        data_id = str(raw.get("data_id") or "")
        status = str(raw.get("status") or "")
        content_hash = str(raw.get("content_hash") or "")
        if not data_id or status != "READY" or not content_hash.startswith("sha256:"):
            raise ValueError("registered context receipt is invalid")
        value = {"data_id": data_id, "status": status, "content_hash": content_hash}
        existing = result.get(data_id)
        if existing is not None and existing != value:
            raise ValueError("conflicting registered context receipts")
        result[data_id] = value
    return result


def _mapping(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("coverage component must be a JSON object")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compose Research500 coverage receipts")
    parser.add_argument("--base-inputs", required=True)
    parser.add_argument("--financial-freeze", required=True)
    parser.add_argument("--enrichment-receipt", required=True)
    parser.add_argument("--auxiliary-result", required=True)
    parser.add_argument("--registered-receipt")
    parser.add_argument("--representative-plan")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    value = compose_coverage_inputs(
        base_inputs_path=args.base_inputs,
        financial_freeze_path=args.financial_freeze,
        enrichment_receipt_path=args.enrichment_receipt,
        auxiliary_result_path=args.auxiliary_result,
        registered_receipt_path=args.registered_receipt,
        representative_plan_path=args.representative_plan,
        output_path=args.output,
    )
    print(json.dumps({"status": "PASSED", "artifact_hash": value["artifact_hash"]}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["compose_coverage_inputs"]
