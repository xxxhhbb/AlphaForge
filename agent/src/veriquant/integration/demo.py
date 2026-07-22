"""Command-line entry point for the VeriQuant A-F offline acceptance chain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .e2e import IntegrationInvariantError, run_offline_acceptance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run VeriQuant A-F integration acceptance")
    parser.add_argument("--offline", action="store_true", help="required fail-closed offline mode")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.offline:
        print(json.dumps({"status": "FAILED", "code": "OFFLINE_FLAG_REQUIRED"}, sort_keys=True))
        return 2
    repo_root = Path(__file__).resolve().parents[4]
    output = args.output_dir or (
        repo_root / ".autobuild" / "veriquant" / "integration" / f"v1-offline-seed-{args.seed}"
    )
    try:
        result = run_offline_acceptance(
            output_root=output,
            seed=args.seed,
            workbench_root=repo_root / ".autobuild" / "veriquant" / "integration" / "workbench_artifacts",
            run_id=args.run_id,
        )
    except (IntegrationInvariantError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "FAILED", "code": exc.__class__.__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    print(f"Markdown report: {output / 'report.md'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
