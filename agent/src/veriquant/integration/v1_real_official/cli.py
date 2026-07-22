"""Offline CLI for the pinned real approved-proxy visible Evidence run."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from veriquant.contracts import ContractValidationError
from veriquant.tushare_market_state.models import MarketStateError

from .service import RealOfficialEvidenceAdapter


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def _default_output() -> Path:
    return _repo_root() / ".autobuild/veriquant/parallel/ui_agent_c/real_official_run"


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
    parser = argparse.ArgumentParser(
        description="Compute visible Official Evidence from the pinned approved-proxy snapshot"
    )
    parser.add_argument("--output-root", type=Path, default=_default_output())
    parser.add_argument("--seed", type=int, default=7)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output_root.resolve()
    allowed = (_repo_root() / ".autobuild/veriquant/parallel/ui_agent_c").resolve()
    try:
        output.relative_to(allowed)
    except ValueError:
        print(json.dumps({"status": "FAILED", "failure_code": "UNAUTHORIZED_OUTPUT_PATH"}))
        return 2
    try:
        result = RealOfficialEvidenceAdapter().run(output_root=output, seed=args.seed)
        payload = {**result.to_dict(), "artifact_hash": result.artifact_hash}
        _atomic_json(output / "official_evidence_run.json", payload)
        print(
            json.dumps(
                {
                    "status": result.status,
                    "artifact_hash": result.artifact_hash,
                    "official_evidence_run": result.official_evidence_run,
                    "promotion_run": result.promotion_run,
                    "challenger_created": result.challenger_created,
                },
                sort_keys=True,
            )
        )
        return 0 if result.official_evidence_run else 1
    except (ContractValidationError, MarketStateError, OSError, ValueError) as exc:
        code = getattr(getattr(exc, "code", None), "value", None) or type(exc).__name__
        payload = {
            "schema_version": "veriquant.real-official-evidence-cli/v1",
            "status": "FAILED",
            "failure_code": code,
            "failure_type": type(exc).__name__,
            "message": "Pinned real visible Evidence run failed closed.",
            "official_evidence_run": False,
            "sealed_evidence_run": False,
            "promotion_run": False,
            "challenger_created": False,
        }
        _atomic_json(output / "official_evidence_run.json", payload)
        print(json.dumps(payload, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

