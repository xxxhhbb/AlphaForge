"""Bounded CLI for the compatible-proxy real-data auxiliary integration."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from veriquant.contracts import ContractValidationError
from veriquant.data import canonical_content_hash
from veriquant.tushare_market_state.models import MarketStateError
from veriquant.tushare_sdk_proxy.client import ProxySdkConfig, TushareProxyError, create_proxy_client

from .v1_real_data_chain import PROXY_HOST, PROXY_PATH, run_real_auxiliary_chain


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _paths() -> dict[str, Path]:
    root = _repo_root()
    stage = root / ".autobuild/veriquant/integration/v1_tushare_proxy_stage"
    snapshot_hash = "dd9b48413fae1c89c52e54b5f52cf7ef46de88fd3d98fc6bae776a1a30cd527a"
    capture_hash = "fcd43df20177dfc6df425c2dbafa5855639905831fbe14c88be177296fefb2d7"
    return {
        "env": root / "agent/.env",
        "manifest": stage / "snapshots/tushare-proxy-operational-smoke" / snapshot_hash / "manifest.json",
        "snapshot_root": stage / "snapshots",
        "calendar_root": root / ".autobuild/veriquant/calendars",
        "bar_capture": stage / "captures" / capture_hash / "capture.json",
        "market_state_root": root / ".autobuild/veriquant/integration/v1_real_data/market_state",
        "result": root / ".autobuild/veriquant/integration/v1_real_data/real_auxiliary_smoke.json",
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=str(path.parent), suffix=".tmp"
    )
    temporary = Path(handle.name)
    try:
        json.dump(payload, handle, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the bounded VeriQuant real-data auxiliary slice")
    parser.add_argument("--enable-network", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = _paths()
    if not args.enable_network:
        payload = {
            "schema_version": "veriquant.integration-real-data-cli/v1",
            "status": "OFFLINE_DISABLED",
            "network_used": False,
            "authority_class": "AUXILIARY_RESEARCH_EVIDENCE",
            "official_evidence_run": False,
            "promotion_run": False,
            "challenger_created": False,
            "real_shadow_replay_run": False,
        }
        _atomic_json(paths["result"], payload)
        print(json.dumps(payload, sort_keys=True))
        return 0
    try:
        settings = dotenv_values(paths["env"])
        config = ProxySdkConfig(
            endpoint=(settings.get("TUSHARE_PROXY_URL") or "").strip(),
            token=(settings.get("TUSHARE_TOKEN") or "").strip(),
        )
        bar_capture = json.loads(paths["bar_capture"].read_text(encoding="utf-8"))
        result = run_real_auxiliary_chain(
            sdk=create_proxy_client(config),
            manifest_path=paths["manifest"],
            snapshot_root=paths["snapshot_root"],
            calendar_root=paths["calendar_root"],
            market_state_root=paths["market_state_root"],
            bar_capture_hash=canonical_content_hash(bar_capture),
            endpoint_host=PROXY_HOST,
            endpoint_path=PROXY_PATH,
            network_used=True,
        )
        payload = {**result.to_dict(), "artifact_hash": result.artifact_hash}
        _atomic_json(paths["result"], payload)
        print(json.dumps(payload, sort_keys=True))
        return 0
    except (ContractValidationError, MarketStateError, TushareProxyError, OSError, ValueError) as exc:
        code = getattr(getattr(exc, "code", None), "value", None) or type(exc).__name__
        payload = {
            "schema_version": "veriquant.integration-real-data-cli/v1",
            "status": "FAILED",
            "failure_code": code,
            "failure_type": type(exc).__name__,
            "message": "Bounded compatible-proxy integration failed closed.",
            "network_used": True,
            "authority_class": "AUXILIARY_RESEARCH_EVIDENCE",
            "official_evidence_run": False,
            "promotion_run": False,
            "challenger_created": False,
            "real_shadow_replay_run": False,
        }
        _atomic_json(paths["result"], payload)
        print(json.dumps(payload, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
