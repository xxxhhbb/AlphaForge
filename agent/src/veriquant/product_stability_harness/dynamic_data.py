"""Recoverable Harness wave for Vibe dynamic research data.

The wave imports a completed real AgentLoop outcome, recovers each immutable
tool envelope from the content store, applies conservative publication clocks,
and materializes a PIT-certified auxiliary dataset.  It never calls a second
research runtime and never upgrades dynamic tool data to official authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_agent_runtime import InvestigationReceipt
from ..research_data_gateway.dynamic_tool_freeze import DynamicVibeToolFreezeHarness
from ..research_data_gateway.models import canonical_hash
from ..research_toolbox.handles import ContentAddressedHandleStore


class DynamicDataQualificationHarness:
    def __init__(
        self,
        *,
        harness_root: str | Path,
        agent_run_root: str | Path,
        base_data_root: str | Path,
        output_data_root: str | Path,
        specialist_id: str = "alternative_data",
    ) -> None:
        self.root = Path(harness_root).resolve()
        self.agent_run_root = Path(agent_run_root).resolve()
        self.base_data_root = Path(base_data_root).resolve()
        self.output_data_root = Path(output_data_root).resolve()
        self.specialist_id = specialist_id
        self.root.mkdir(parents=True, exist_ok=True)

    def run(self) -> Mapping[str, Any]:
        cached = self._load_valid_final()
        if cached is not None:
            return cached
        self._write_progress("IN_PROGRESS", "IMPORT_AGENT_TOOL_RECEIPTS")
        base = FrozenValidationDataRepository(self.base_data_root).load(require_strict_provenance=True)
        if base is None:
            return self._blocked("BASE_DATA_UNAVAILABLE", "IMPORT_AGENT_TOOL_RECEIPTS")
        try:
            outcome_path = self._outcome_path()
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            payload = outcome.get("payload") if isinstance(outcome, Mapping) else None
            if not isinstance(payload, Mapping):
                raise ValueError("agent outcome payload is malformed")
            raw_receipts = payload.get("investigation_receipts")
            if not isinstance(raw_receipts, list):
                raise ValueError("agent outcome has no investigation receipts")
            receipts = tuple(sorted(
                (
                    InvestigationReceipt(
                        tool_name=str(item["tool_name"]),
                        arguments_hash=str(item["arguments_hash"]),
                        result_hash=str(item["result_hash"]),
                        available_at=datetime.fromisoformat(str(item["available_at"])),
                    )
                    for item in raw_receipts
                    if isinstance(item, Mapping)
                    and str(item.get("tool_name", "")).startswith("get_")
                ),
                key=lambda item: (item.tool_name, item.arguments_hash, item.result_hash),
            ))
            receipts = tuple(
                item for item in receipts
                if item.tool_name in {"get_stock_news", "get_research_reports"}
            )
            if not receipts:
                return self._blocked("DYNAMIC_TOOL_RECEIPTS_MISSING", "IMPORT_AGENT_TOOL_RECEIPTS")
            store = ContentAddressedHandleStore(
                self.agent_run_root / "research_tool_outputs" / self.specialist_id
            )
            self._write_progress("IN_PROGRESS", "FREEZE_AND_PIT_CERTIFY")
            freezer = DynamicVibeToolFreezeHarness()
            bundle = freezer.freeze(
                snapshot=base.snapshot,
                receipts=receipts,
                handle_store=store,
            )
            _, authority = freezer.materialize(
                base=base,
                bundle=bundle,
                output_root=self.output_data_root,
            )
        except Exception as exc:
            return self._blocked(_typed_failure(exc), "FREEZE_AND_PIT_CERTIFY")
        temporal_count = int(authority["temporally_covered_symbol_count"])
        status = "PASSED" if temporal_count > 0 else "PARTIAL"
        failures = [] if temporal_count > 0 else ["TOOL_DATA_OUTSIDE_EVIDENCE_WINDOW"]
        final: dict[str, Any] = {
            "schema_version": "alphaforge.dynamic-data-qualification-harness/v1",
            "status": status,
            "current_stage": "COMPLETED",
            "failure_codes": failures,
            "agent_outcome_path": str(outcome_path),
            "base_snapshot_hash": base.snapshot.artifact_hash,
            "dynamic_bundle_hash": bundle.artifact_hash,
            "materialized_snapshot_hash": authority["snapshot_hash"],
            "authority_manifest_hash": authority["artifact_hash"],
            "authority_ceiling": "AUXILIARY_ONLY",
            "receipt_count": len(receipts),
            "queried_symbol_count": authority["queried_symbol_count"],
            "temporally_covered_symbol_count": temporal_count,
            "document_count": authority["document_count"],
            "output_data_root": str(self.output_data_root),
            "recovery_cursor": "COMPLETE",
        }
        final["artifact_hash"] = canonical_hash(final)
        _atomic_json(self.root / "final_verdict.json", final)
        self._write_progress(status, "COMPLETED", artifact_hash=final["artifact_hash"])
        return final

    def _outcome_path(self) -> Path:
        candidates = sorted(self.agent_run_root.glob("*/branches/task_alternative_data/outcome.json"))
        if not candidates:
            candidates = sorted(self.agent_run_root.glob("**/branches/task_alternative_data/outcome.json"))
        if len(candidates) != 1:
            raise ValueError("agent run root has ambiguous alternative-data outcome")
        return candidates[0]

    def _load_valid_final(self) -> Mapping[str, Any] | None:
        path = self.root / "final_verdict.json"
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        claimed = raw.pop("artifact_hash", None)
        if claimed != canonical_hash(raw):
            return None
        if raw.get("recovery_cursor") != "COMPLETE":
            return None
        return {**raw, "artifact_hash": claimed, "replayed_without_provider": True}

    def _blocked(self, code: str, cursor: str) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "alphaforge.dynamic-data-qualification-harness/v1",
            "status": "BLOCKED",
            "current_stage": cursor,
            "failure_codes": [code],
            "authority_ceiling": "AUXILIARY_ONLY",
            "recovery_cursor": cursor,
        }
        payload["artifact_hash"] = canonical_hash(payload)
        _atomic_json(self.root / "final_verdict.json", payload)
        self._write_progress("BLOCKED", cursor, failure_code=code)
        return payload

    def _write_progress(self, status: str, cursor: str, **extra: Any) -> None:
        payload = {
            "schema_version": "alphaforge.dynamic-data-qualification-progress/v1",
            "status": status,
            "recovery_cursor": cursor,
            **extra,
        }
        payload["artifact_hash"] = canonical_hash(payload)
        _atomic_json(self.root / "progress.json", payload)


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


def _typed_failure(exc: Exception) -> str:
    """Map only our fixed diagnostics; never serialize provider exceptions."""
    text = str(exc)
    known = {
        "dynamic tool envelope is malformed": "DYNAMIC_ENVELOPE_MALFORMED",
        "dynamic tool receipt name mismatch": "DYNAMIC_TOOL_IDENTITY_MISMATCH",
        "dynamic tool arguments identity mismatch": "DYNAMIC_ARGUMENTS_IDENTITY_MISMATCH",
        "dynamic tool envelope attempted an authority upgrade": "DYNAMIC_AUTHORITY_ESCALATION",
        "dynamic tool payload handle is missing": "DYNAMIC_PAYLOAD_HANDLE_MISSING",
        "dynamic tool payload is malformed": "DYNAMIC_PAYLOAD_MALFORMED",
        "dynamic tool receipt clock mismatch": "DYNAMIC_CLOCK_IDENTITY_MISMATCH",
        "unsupported provider publication time format": "DYNAMIC_TIME_FORMAT_UNSUPPORTED",
        "date-only document has no later frozen trading session": "DYNAMIC_DATE_OUTSIDE_CALENDAR",
        "unknown content handle": "DYNAMIC_CONTENT_HANDLE_MISSING",
        "content handle failed integrity verification": "DYNAMIC_CONTENT_TAMPERED",
    }
    return known.get(text, type(exc).__name__.upper())


__all__ = ["DynamicDataQualificationHarness"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze real Vibe tool receipts into an auxiliary PIT dataset.")
    parser.add_argument("--harness-root", type=Path, required=True)
    parser.add_argument("--agent-run-root", type=Path, required=True)
    parser.add_argument("--base-data-root", type=Path, required=True)
    parser.add_argument("--output-data-root", type=Path, required=True)
    args = parser.parse_args(argv)
    result = DynamicDataQualificationHarness(
        harness_root=args.harness_root,
        agent_run_root=args.agent_run_root,
        base_data_root=args.base_data_root,
        output_data_root=args.output_data_root,
    ).run()
    print(json.dumps(dict(result), ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"PASSED", "PARTIAL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
