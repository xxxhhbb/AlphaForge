"""Freeze Vibe investigation receipts into PIT-certified auxiliary features.

This module does not reimplement any Vibe data tool.  It starts from the
content-addressed receipt and sanitized payload already produced by
``VibeResearchToolbox``.  It verifies that immutable chain, applies conservative
source-time policies, and projects only actually queried symbols into a new
frozen validation dataset.  Because the external news/report sources are not
approved official market inputs, the resulting dataset carries an explicit
``AUXILIARY_ONLY`` authority ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..data import MarketSnapshot, SnapshotRow
from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_agent_runtime import InvestigationReceipt
from ..research_toolbox.handles import ContentAddressedHandleStore
from ..research_validation_pipeline_v2.models import FrozenValidationData
from .models import canonical_hash


SCHEMA_VERSION = "alphaforge.dynamic-vibe-freeze/v1"
AUTHORITY_FILE = "research_data_authority.json"
_TOOLS = frozenset({"get_stock_news", "get_research_reports"})
_FEATURES = ("news_count_20_sessions", "research_report_count_60_sessions")


@dataclass(frozen=True, slots=True)
class FrozenDynamicDocument:
    tool_name: str
    symbol: str
    published_at: datetime
    available_at: datetime
    source_hash: str

    def __post_init__(self) -> None:
        if self.tool_name not in _TOOLS:
            raise ValueError("unsupported dynamic document tool")
        if not self.symbol or "." not in self.symbol:
            raise ValueError("dynamic document requires a market symbol")
        if self.published_at.tzinfo is None or self.available_at.tzinfo is None:
            raise ValueError("dynamic document clocks must be timezone-aware")
        if self.published_at > self.available_at:
            raise ValueError("published_at follows available_at")
        if not self.source_hash.startswith("sha256:"):
            raise ValueError("dynamic document source hash is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "symbol": self.symbol,
            "published_at": self.published_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True, slots=True)
class DynamicToolFreezeBundle:
    base_snapshot_hash: str
    receipt_hashes: tuple[str, ...]
    queried_symbols: tuple[str, ...]
    documents: tuple[FrozenDynamicDocument, ...]
    availability_policy_hash: str
    authority_ceiling: str = "AUXILIARY_ONLY"

    def __post_init__(self) -> None:
        if not self.base_snapshot_hash.startswith("sha256:"):
            raise ValueError("base snapshot hash is invalid")
        if self.authority_ceiling != "AUXILIARY_ONLY":
            raise ValueError("dynamic Vibe data cannot claim official authority")
        if tuple(sorted(set(self.receipt_hashes))) != self.receipt_hashes:
            raise ValueError("receipt hashes must be unique and stable-sorted")
        if tuple(sorted(set(self.queried_symbols))) != self.queried_symbols:
            raise ValueError("queried symbols must be unique and stable-sorted")
        ordered = tuple(sorted(
            self.documents,
            key=lambda item: (item.available_at, item.symbol, item.tool_name, item.source_hash),
        ))
        if ordered != self.documents:
            raise ValueError("documents must be stable-sorted")
        if any(item.symbol not in self.queried_symbols for item in self.documents):
            raise ValueError("document symbol was not in the frozen query scope")

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "base_snapshot_hash": self.base_snapshot_hash,
            "receipt_hashes": list(self.receipt_hashes),
            "queried_symbols": list(self.queried_symbols),
            "documents": [item.to_dict() for item in self.documents],
            "availability_policy_hash": self.availability_policy_hash,
            "authority_ceiling": self.authority_ceiling,
        }
        if include_hash:
            payload["artifact_hash"] = canonical_hash(payload)
        return payload

    @property
    def artifact_hash(self) -> str:
        return str(self.to_dict()["artifact_hash"])


class DynamicVibeToolFreezeHarness:
    """Recover receipts, validate clocks, and materialize an auxiliary PIT panel."""

    def freeze(
        self,
        *,
        snapshot: MarketSnapshot,
        receipts: Sequence[InvestigationReceipt],
        handle_store: ContentAddressedHandleStore,
    ) -> DynamicToolFreezeBundle:
        sessions = tuple(sorted({row.event_time for row in snapshot.rows}))
        if not sessions:
            raise ValueError("base snapshot has no sessions")
        documents: list[FrozenDynamicDocument] = []
        queried: set[str] = set()
        receipt_hashes: set[str] = set()
        for receipt in sorted(receipts, key=lambda item: (item.tool_name, item.arguments_hash, item.result_hash)):
            if receipt.tool_name not in _TOOLS:
                continue
            envelope = handle_store.read_json(receipt.result_hash)
            if not isinstance(envelope, Mapping):
                raise ValueError("dynamic tool envelope is malformed")
            if envelope.get("tool_name") != receipt.tool_name:
                raise ValueError("dynamic tool receipt name mismatch")
            if envelope.get("arguments_hash") != receipt.arguments_hash:
                raise ValueError("dynamic tool arguments identity mismatch")
            if envelope.get("authority") != "AUXILIARY_RESEARCH_EVIDENCE" or envelope.get("official_eligible") is not False:
                raise ValueError("dynamic tool envelope attempted an authority upgrade")
            artifact = envelope.get("artifact")
            if not isinstance(artifact, Mapping) or not isinstance(artifact.get("handle_id"), str):
                raise ValueError("dynamic tool payload handle is missing")
            payload = handle_store.read_json(str(artifact["handle_id"]))
            if not isinstance(payload, Mapping):
                raise ValueError("dynamic tool payload is malformed")
            fetched_at = _parse_aware(str(envelope.get("fetched_at")))
            if fetched_at != receipt.available_at:
                raise ValueError("dynamic tool receipt clock mismatch")
            parsed, scope = _documents_from_payload(
                tool_name=receipt.tool_name,
                payload=payload,
                fetched_at=fetched_at,
                sessions=sessions,
            )
            documents.extend(parsed)
            queried.update(scope)
            receipt_hashes.add(receipt.result_hash)
        policy = canonical_hash({
            "schema_version": SCHEMA_VERSION,
            "timestamp_policy": "EXACT_PROVIDER_TIMESTAMP_OR_NEXT_FROZEN_SESSION_CLOSE",
            "authority": "AUXILIARY_ONLY",
        })
        return DynamicToolFreezeBundle(
            base_snapshot_hash=snapshot.artifact_hash,
            receipt_hashes=tuple(sorted(receipt_hashes)),
            queried_symbols=tuple(sorted(queried)),
            documents=tuple(sorted(
                documents,
                key=lambda item: (item.available_at, item.symbol, item.tool_name, item.source_hash),
            )),
            availability_policy_hash=policy,
        )

    def materialize(
        self,
        *,
        base: FrozenValidationData,
        bundle: DynamicToolFreezeBundle,
        output_root: str | Path,
    ) -> tuple[FrozenValidationData, Mapping[str, Any]]:
        if bundle.base_snapshot_hash != base.snapshot.artifact_hash:
            raise ValueError("dynamic bundle belongs to another base snapshot")
        snapshot = _project_document_features(base.snapshot, bundle)
        value = FrozenValidationData(
            snapshot=snapshot,
            labels=dict(base.labels),
            qualification_hash=canonical_hash({
                "base_qualification_hash": base.qualification_hash,
                "dynamic_bundle_hash": bundle.artifact_hash,
                "snapshot_hash": snapshot.artifact_hash,
            }),
            source_label=base.source_label,
            row_provenance=base.row_provenance,
        )
        root = Path(output_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        repository_hash = FrozenValidationDataRepository(root).save(value)
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "READY",
            "authority_ceiling": "AUXILIARY_ONLY",
            "authority_reason": "DYNAMIC_VIBE_TOOL_DATA_NOT_APPROVED_FOR_OFFICIAL_EVIDENCE",
            "base_snapshot_hash": base.snapshot.artifact_hash,
            "dynamic_bundle_hash": bundle.artifact_hash,
            "snapshot_hash": snapshot.artifact_hash,
            "repository_hash": repository_hash,
            "feature_fields": list(_FEATURES),
            "queried_symbol_count": len(bundle.queried_symbols),
            "temporally_covered_symbol_count": len({
                item.symbol
                for item in bundle.documents
                if base.snapshot.start_time <= item.available_at <= base.snapshot.decision_time
            }),
            "document_count": len(bundle.documents),
            "availability_policy_hash": bundle.availability_policy_hash,
        }
        manifest["artifact_hash"] = canonical_hash(manifest)
        _atomic_json(root / AUTHORITY_FILE, manifest)
        _atomic_json(root / "dynamic_tool_freeze_bundle.json", bundle.to_dict())
        return value, MappingProxyType(manifest)


def _documents_from_payload(
    *,
    tool_name: str,
    payload: Mapping[str, Any],
    fetched_at: datetime,
    sessions: tuple[datetime, ...],
) -> tuple[list[FrozenDynamicDocument], set[str]]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return [], set()
    raw_symbol = data.get("code")
    symbol = str(raw_symbol).strip().upper() if isinstance(raw_symbol, str) else ""
    if not symbol or "." not in symbol:
        return [], set()
    queried = {symbol}
    key = "articles" if tool_name == "get_stock_news" else "reports"
    time_key = "published" if tool_name == "get_stock_news" else "publish_date"
    values = data.get(key)
    if not isinstance(values, list):
        return [], queried
    output: list[FrozenDynamicDocument] = []
    for raw in values:
        if not isinstance(raw, Mapping) or not isinstance(raw.get(time_key), str):
            continue
        published, available = _provider_clocks(str(raw[time_key]), sessions, fetched_at)
        if available > fetched_at:
            # A historical provider row cannot have been knowable after the
            # immutable fetch receipt.  Ignore it rather than inventing time.
            continue
        output.append(FrozenDynamicDocument(
            tool_name=tool_name,
            symbol=symbol,
            published_at=published,
            available_at=available,
            source_hash=canonical_hash(dict(raw)),
        ))
    return output, queried


def _provider_clocks(
    value: str,
    sessions: tuple[datetime, ...],
    fetched_at: datetime,
) -> tuple[datetime, datetime]:
    text = value.strip()
    session_tz = sessions[0].tzinfo
    if len(text) == 19:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=session_tz)
        return parsed, parsed
    if len(text) == 10:
        date_value = datetime.fromisoformat(text).date()
        candidates = [item for item in sessions if item.date() > date_value]
        published = datetime.combine(date_value, datetime.min.time(), tzinfo=session_tz)
        # If the document is newer than the historical market spine, do not
        # backdate it into that spine.  The immutable fetch time is a safe,
        # conservative availability bound; projection will correctly leave
        # all earlier market rows missing.
        return published, candidates[0] if candidates else fetched_at
    raise ValueError("unsupported provider publication time format")


def _project_document_features(
    snapshot: MarketSnapshot,
    bundle: DynamicToolFreezeBundle,
) -> MarketSnapshot:
    fields = tuple(snapshot.fields) + tuple(field for field in _FEATURES if field not in snapshot.fields)
    sessions = tuple(sorted({row.event_time for row in snapshot.rows}))
    session_index = {value: index for index, value in enumerate(sessions)}
    by_symbol: dict[str, list[FrozenDynamicDocument]] = {}
    for item in bundle.documents:
        by_symbol.setdefault(item.symbol, []).append(item)
    covered_symbols = {
        item.symbol
        for item in bundle.documents
        if snapshot.start_time <= item.available_at <= snapshot.decision_time
    }
    rows: list[SnapshotRow] = []
    for row in snapshot.rows:
        values = dict(row.values)
        if row.symbol not in covered_symbols:
            values["news_count_20_sessions"] = None
            values["research_report_count_60_sessions"] = None
        else:
            position = session_index[row.event_time]
            news_start = sessions[max(0, position - 19)]
            report_start = sessions[max(0, position - 59)]
            documents = by_symbol.get(row.symbol, ())
            values["news_count_20_sessions"] = sum(
                item.tool_name == "get_stock_news"
                and news_start <= item.available_at <= row.event_time
                for item in documents
            )
            values["research_report_count_60_sessions"] = sum(
                item.tool_name == "get_research_reports"
                and report_start <= item.available_at <= row.event_time
                for item in documents
            )
        rows.append(SnapshotRow(
            event_time=row.event_time,
            symbol=row.symbol,
            values={field: values.get(field) for field in fields},
        ))
    coverage = dict(snapshot.coverage_by_field)
    total = len(rows)
    for field in _FEATURES:
        coverage[field] = (
            sum(item.values.get(field) is not None for item in rows) / total
            if total else 0.0
        )
    return MarketSnapshot(
        dataset_id=f"{snapshot.dataset_id}_dynamic_aux_v1",
        decision_time=snapshot.decision_time,
        start_time=snapshot.start_time,
        fields=fields,
        rows=tuple(rows),
        historical_universe=dict(snapshot.historical_universe),
        coverage_by_field=coverage,
    )


def load_authority_manifest(data_root: str | Path, *, snapshot_hash: str) -> Mapping[str, Any] | None:
    path = Path(data_root).resolve() / AUTHORITY_FILE
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("research data authority manifest is malformed")
    claimed = raw.pop("artifact_hash", None)
    if claimed != canonical_hash(raw):
        raise ValueError("research data authority manifest hash mismatch")
    if raw.get("snapshot_hash") != snapshot_hash:
        raise ValueError("research data authority manifest belongs to another snapshot")
    if raw.get("authority_ceiling") != "AUXILIARY_ONLY":
        raise ValueError("unsupported research data authority ceiling")
    return MappingProxyType({**raw, "artifact_hash": claimed})


def _parse_aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("dynamic tool envelope clock is not timezone-aware")
    return parsed


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=path.parent)
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


__all__ = [
    "AUTHORITY_FILE",
    "DynamicToolFreezeBundle",
    "DynamicVibeToolFreezeHarness",
    "FrozenDynamicDocument",
    "load_authority_manifest",
]
