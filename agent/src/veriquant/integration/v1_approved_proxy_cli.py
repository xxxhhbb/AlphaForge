"""Apply the pinned operator proxy approval to an existing frozen real-data run."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from veriquant.contracts import ContractValidationError

from .v1_approved_proxy_policy import (
    APPROVED_PROXY_POLICY_HASH,
    load_operator_proxy_approval,
    qualify_approved_proxy_inputs,
)
from .v1_real_data_chain import RealAuxiliaryChainResult


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _paths() -> dict[str, Path]:
    root = _repo_root()
    stage = root / ".autobuild/veriquant/integration/v1_real_data"
    return {
        "auxiliary": stage / "real_auxiliary_smoke.json",
        "qualification": stage / "approved_proxy_qualification.json",
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


def _load_auxiliary_result(path: Path) -> RealAuxiliaryChainResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("frozen auxiliary result is unavailable or invalid") from exc
    recorded_hash = payload.pop("artifact_hash", None)
    result = RealAuxiliaryChainResult.from_payload(payload)
    if recorded_hash != result.artifact_hash:
        raise ValueError("frozen auxiliary result hash does not match its payload")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply the pinned V1 proxy data-vendor approval")
    parser.add_argument("--apply-operator-approval", action="store_true")
    parser.add_argument("--policy-hash", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = _paths()
    if not args.apply_operator_approval:
        payload = {
            "schema_version": "veriquant.approved-proxy-cli/v1",
            "status": "APPROVAL_NOT_APPLIED",
            "network_used": False,
            "official_tushare_source": False,
            "official_evidence_run": False,
            "promotion_run": False,
            "challenger_created": False,
        }
        _atomic_json(paths["qualification"], payload)
        print(json.dumps(payload, sort_keys=True))
        return 0
    try:
        if args.policy_hash != APPROVED_PROXY_POLICY_HASH:
            raise ValueError("explicit policy hash is missing or does not match the pinned approval")
        auxiliary = _load_auxiliary_result(paths["auxiliary"])
        approval = load_operator_proxy_approval()
        qualification = qualify_approved_proxy_inputs(result=auxiliary, approval=approval)
        payload = {**qualification.to_dict(), "artifact_hash": qualification.artifact_hash}
        _atomic_json(paths["qualification"], payload)
        print(json.dumps(payload, sort_keys=True))
        return 0
    except (ContractValidationError, OSError, ValueError) as exc:
        code = getattr(getattr(exc, "code", None), "value", None) or type(exc).__name__
        payload = {
            "schema_version": "veriquant.approved-proxy-cli/v1",
            "status": "FAILED",
            "failure_code": code,
            "message": "Approved proxy qualification failed closed.",
            "network_used": False,
            "official_tushare_source": False,
            "official_evidence_run": False,
            "promotion_run": False,
            "challenger_created": False,
        }
        _atomic_json(paths["qualification"], payload)
        print(json.dumps(payload, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
