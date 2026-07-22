"""Offline CLI for the Level-1 research coverage harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .harness import CoverageInputs, Research500Level1Harness


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--coverage-input")
    args = parser.parse_args()
    inputs = CoverageInputs()
    if args.coverage_input:
        raw = json.loads(Path(args.coverage_input).read_text(encoding="utf-8"))
        inputs = CoverageInputs.from_mapping(raw)
    verdict = Research500Level1Harness().run(
        catalog_path=args.catalog,
        output_dir=args.output,
        coverage_inputs=inputs,
    )
    print(json.dumps(verdict, ensure_ascii=False, sort_keys=True))
    return 0 if verdict["level1_static_status"] == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
