"""Mechanical acceptance harness for the ResearchDataGateway."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from .models import GatewayStatus, ResearchDataGatewayResult, canonical_hash
from ..dynamic_acquisition import stable_content_hash


REQUIRED_OFFICIAL = frozenset({
    "stock_basic", "trade_cal", "daily", "adj_factor", "daily_basic",
    "fina_indicator", "income", "balancesheet", "cashflow", "moneyflow",
    "index_daily", "shibor", "forecast", "express",
})
REQUIRED_AUXILIARY = frozenset({"northbound", "dragon_tiger", "stock_news", "research_reports"})


@dataclass(frozen=True, slots=True)
class GatewayAcceptanceVerdict:
    status: str
    checks: Mapping[str, bool]
    failures: tuple[str, ...]
    input_manifest_hash: str

    @property
    def artifact_hash(self) -> str:
        return canonical_hash({
            "status": self.status,
            "checks": dict(sorted(self.checks.items())),
            "failures": self.failures,
            "input_manifest_hash": self.input_manifest_hash,
        })


class ResearchDataGatewayHarness:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def evaluate(self, result: ResearchDataGatewayResult, *, require_auxiliary: bool = True) -> GatewayAcceptanceVerdict:
        manifest = result.manifest
        by_id = {item.source_id: item for item in manifest.receipts}
        official_terminal = all(by_id.get(item) is not None and by_id[item].status in {"SUCCESS", "EMPTY"} for item in REQUIRED_OFFICIAL)
        official_nonempty = all(
            by_id.get(item) is not None
            and by_id[item].status == "SUCCESS"
            and by_id[item].row_count > 0
            for item in REQUIRED_OFFICIAL
        )
        auxiliary_connected = all(by_id.get(item) is not None for item in REQUIRED_AUXILIARY)
        auxiliary_authority = all(by_id[item].authority.value == "AUXILIARY_RESEARCH_EVIDENCE" for item in REQUIRED_AUXILIARY if item in by_id)
        auxiliary_nonempty = all(
            by_id.get(item) is not None
            and by_id[item].status == "SUCCESS"
            and by_id[item].row_count > 0
            for item in REQUIRED_AUXILIARY
        )
        checks = {
            "gateway_ready": manifest.status is GatewayStatus.READY,
            "historical_universe_nonempty": manifest.historical_symbol_count > 0,
            "sse_szse_calendars": manifest.calendar_exchanges == ("SSE", "SZSE"),
            "official_sources_executed": official_terminal,
            # An EMPTY endpoint proves only that a request terminated.  It
            # cannot prove the data family is usable by a real experiment.
            "official_sources_nonempty": official_nonempty,
            "approved_proxy_identity": manifest.source_label == "APPROVED_PROXY_DATA_VENDOR" and not manifest.official_tushare,
            "auxiliary_connectors_present": auxiliary_connected if require_auxiliary else True,
            "auxiliary_authority_isolated": auxiliary_authority,
            "auxiliary_sources_nonempty": auxiliary_nonempty if require_auxiliary else True,
            "official_freeze_hashed": bool(manifest.official_freeze_hash),
        }
        failures = tuple(key for key, passed in checks.items() if not passed)
        verdict = GatewayAcceptanceVerdict(
            status="PASSED" if not failures else "FAILED",
            checks=checks,
            failures=failures,
            input_manifest_hash=manifest.artifact_hash,
        )
        _atomic_json(self.root / "acceptance.json", {
            "status": verdict.status,
            "checks": dict(sorted(verdict.checks.items())),
            "failures": list(verdict.failures),
            "input_manifest_hash": verdict.input_manifest_hash,
            "artifact_hash": verdict.artifact_hash,
        })
        return verdict

    def evaluate_existing_reference(
        self,
        freeze_root: str | Path,
        *,
        start_date: str,
        end_date: str,
    ) -> Mapping[str, Any]:
        """Prove a real frozen L/D/P universe and dual-exchange calendar."""
        from .gateway import discover_historical_symbols

        source = Path(freeze_root).resolve()
        raw = json.loads((source / "freeze_manifest.json").read_text(encoding="utf-8"))
        claimed = raw.pop("artifact_hash", None)
        if claimed != stable_content_hash(raw):
            raise ValueError("real reference freeze manifest hash mismatch")
        partitions = raw.get("reference_partitions", {})
        symbols = discover_historical_symbols(source, start_date=start_date, end_date=end_date)
        checks = {
            "approved_proxy_identity": raw.get("source_label") == "APPROVED_PROXY_DATA_VENDOR" and raw.get("official_tushare") is False,
            "freeze_success": raw.get("status") == "SUCCESS",
            "stock_status_partitions": tuple(partitions.get("stock_basic", ())) == ("L", "D", "P"),
            "dual_exchange_calendars": tuple(partitions.get("trade_cal", ())) == ("SSE", "SZSE"),
            "historical_universe_nonempty": bool(symbols),
        }
        payload = {
            "schema_version": "alphaforge.real-reference-acceptance/v1",
            "status": "PASSED" if all(checks.values()) else "FAILED",
            "checks": dict(sorted(checks.items())),
            "source_manifest_hash": claimed,
            "historical_symbol_count": len(symbols),
            "historical_symbols_hash": stable_content_hash(symbols),
            "start_date": start_date,
            "end_date": end_date,
        }
        payload["artifact_hash"] = canonical_hash(payload)
        _atomic_json(self.root / "real_reference_acceptance.json", payload)
        return payload


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
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


__all__ = ["GatewayAcceptanceVerdict", "ResearchDataGatewayHarness"]
