"""Evidence-driven data coverage resolver for Agent research."""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping

from .contracts import (
    CoverageFailureCode,
    CoverageStatus,
    DataCoverageDecision,
    ExternalApprovalNeed,
    RegisteredContextReceipt,
)
from .registry import (
    AUXILIARY_DATA_IDS,
    OFFICIAL_DATA_IDS,
    REGISTERED_CONTEXT_DATA_IDS,
    validate_required_data,
)


_SUCCESS = frozenset({"READY", "SUCCESS"})


class DataCoverageResolver:
    """Resolve coverage from observed receipts, never from catalog claims."""

    def resolve(
        self,
        *,
        required_data: tuple[str, ...] | list[str],
        capability_report: object | None = None,
        gateway_manifest: object | None = None,
        auxiliary_receipts: Iterable[object] = (),
        registered_context_receipts: Iterable[RegisteredContextReceipt] = (),
        external_approval_needs: Iterable[ExternalApprovalNeed] = (),
    ) -> DataCoverageDecision:
        required = validate_required_data(required_data)
        gateway = _receipts_by_id(_get(gateway_manifest, "receipts", ()))
        auxiliary = _receipts_by_id(auxiliary_receipts)
        auxiliary_proxies = _proxy_receipts_by_target(auxiliary.values())
        registered = {item.data_id: item for item in registered_context_receipts}
        approval = {item.data_id: item for item in external_approval_needs}
        hashes: set[str] = set()
        satisfied: set[str] = set()
        research_only: set[str] = set()
        unresolved: set[str] = set()
        failures: set[CoverageFailureCode] = set()

        _add_artifact_hash(hashes, capability_report)
        _add_artifact_hash(hashes, gateway_manifest)

        capability_ready = _status(capability_report) == "READY"
        gateway_ready = _status(gateway_manifest) == "READY"

        for data_id in required:
            if data_id in approval:
                unresolved.add(data_id)
                failures.add(CoverageFailureCode.EXTERNAL_APPROVAL_REQUIRED)
                continue
            if data_id in OFFICIAL_DATA_IDS:
                receipt = gateway.get(data_id)
                status = _status(receipt)
                proxy = auxiliary_proxies.get(data_id)
                if receipt is None:
                    if proxy is not None:
                        research_only.add(data_id)
                        failures.add(CoverageFailureCode.AUXILIARY_ONLY)
                        _add_hash(hashes, proxy)
                    else:
                        unresolved.add(data_id)
                        failures.add(CoverageFailureCode.REQUIRED_SOURCE_MISSING)
                elif status == "EMPTY":
                    _add_hash(hashes, receipt)
                    if proxy is not None:
                        research_only.add(data_id)
                        failures.add(CoverageFailureCode.AUXILIARY_ONLY)
                        _add_hash(hashes, proxy)
                    else:
                        unresolved.add(data_id)
                        failures.add(CoverageFailureCode.REQUIRED_SOURCE_EMPTY)
                elif status not in _SUCCESS or _authority(receipt) != "OFFICIAL_INPUT":
                    if proxy is not None:
                        research_only.add(data_id)
                        failures.add(CoverageFailureCode.AUXILIARY_ONLY)
                        _add_hash(hashes, proxy)
                    else:
                        unresolved.add(data_id)
                        failures.add(CoverageFailureCode.REQUIRED_SOURCE_FAILED)
                elif capability_ready and gateway_ready:
                    satisfied.add(data_id)
                    _add_hash(hashes, receipt)
                else:
                    research_only.add(data_id)
                    _add_hash(hashes, receipt)
            elif data_id in AUXILIARY_DATA_IDS:
                receipt = auxiliary.get(data_id)
                if (
                    receipt is not None
                    and _status(receipt) in _SUCCESS
                    and _authority(receipt) == "AUXILIARY_RESEARCH_EVIDENCE"
                ):
                    research_only.add(data_id)
                    _add_hash(hashes, receipt)
                    failures.add(CoverageFailureCode.AUXILIARY_ONLY)
                else:
                    unresolved.add(data_id)
                    failures.add(CoverageFailureCode.REQUIRED_SOURCE_MISSING if receipt is None else CoverageFailureCode.REQUIRED_SOURCE_FAILED)
            elif data_id in REGISTERED_CONTEXT_DATA_IDS:
                receipt = registered.get(data_id)
                if receipt is not None and receipt.status == "READY":
                    research_only.add(data_id)
                    hashes.add(receipt.content_hash)
                    failures.add(CoverageFailureCode.AUXILIARY_ONLY)
                else:
                    unresolved.add(data_id)
                    failures.add(CoverageFailureCode.REGISTERED_CONTEXT_MISSING)

        if any(item in approval for item in required):
            status = CoverageStatus.NEEDS_EXTERNAL_APPROVAL
        elif unresolved:
            status = CoverageStatus.UNRESOLVED
        elif research_only:
            status = CoverageStatus.RESEARCH_READY
            if any(item in OFFICIAL_DATA_IDS for item in research_only):
                if gateway_manifest is None:
                    failures.add(CoverageFailureCode.GATEWAY_MANIFEST_MISSING)
                if not capability_ready:
                    failures.add(CoverageFailureCode.CAPABILITY_PROOF_MISSING)
        else:
            status = CoverageStatus.OFFICIAL_READY

        return DataCoverageDecision(
            required_data=required,
            status=status,
            satisfied_data=tuple(sorted(satisfied)),
            unresolved_data=tuple(sorted(unresolved)),
            research_only_data=tuple(sorted(research_only)),
            evidence_hashes=tuple(sorted(hashes)),
            failure_codes=tuple(failures),
        )


def _get(value: object | None, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _status(value: object | None) -> str:
    raw = _get(value, "status", "")
    if isinstance(raw, Enum):
        raw = raw.value
    return str(raw).upper()


def _authority(value: object | None) -> str:
    raw = _get(value, "authority", "")
    if isinstance(raw, Enum):
        raw = raw.value
    return str(raw).upper()


def _receipts_by_id(values: Iterable[object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for value in values:
        source_id = str(_get(value, "source_id", _get(value, "data_id", "")))
        if not source_id or source_id in output:
            raise ValueError("receipts require unique source_id/data_id")
        output[source_id] = value
    return output


def _proxy_receipts_by_target(values: Iterable[object]) -> dict[str, object]:
    """Index explicit auxiliary substitutes without granting official authority."""

    output: dict[str, object] = {}
    for value in values:
        target = str(_get(value, "proxy_for", ""))
        if not target:
            continue
        if target not in OFFICIAL_DATA_IDS:
            raise ValueError("auxiliary proxy target is not an official data id")
        if (
            _status(value) not in _SUCCESS
            or _authority(value) != "AUXILIARY_RESEARCH_EVIDENCE"
            or not str(_get(value, "content_hash", "")).startswith("sha256:")
        ):
            continue
        if target in output:
            raise ValueError("auxiliary proxy targets must be unique")
        output[target] = value
    return output


def _add_hash(output: set[str], receipt: object) -> None:
    value = _get(receipt, "content_hash")
    if isinstance(value, str) and value.startswith("sha256:") and len(value) == 71:
        output.add(value)


def _add_artifact_hash(output: set[str], value: object | None) -> None:
    artifact_hash = _get(value, "artifact_hash")
    if isinstance(artifact_hash, str) and artifact_hash.startswith("sha256:") and len(artifact_hash) == 71:
        output.add(artifact_hash)


__all__ = ["DataCoverageResolver"]
