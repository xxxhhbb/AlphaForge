from __future__ import annotations

import argparse
import json
from pathlib import Path

from .harness import ResearchQualityDeliveryHarness
from .execution import QualificationBatchRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AlphaForge Research Quality & Delivery Harness v1")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--output-root")
    parser.add_argument("--phase", choices=("initialize", "qualification-batch", "qualification-case"), default="initialize")
    parser.add_argument("--batch")
    parser.add_argument("--case")
    parser.add_argument("--allow-new-real-run", action="store_true")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace)
    output_root = (
        workspace / ".autobuild" / "veriquant" / "research_quality_delivery_harness"
        if args.output_root is None else Path(args.output_root)
    )
    if args.phase == "initialize":
        result = ResearchQualityDeliveryHarness(workspace=workspace, output_root=output_root).initialize()
    elif args.phase == "qualification-batch":
        if not args.batch:
            parser.error("--batch is required for qualification-batch")
        result = QualificationBatchRunner(workspace=workspace, root=output_root).run_batch(args.batch)
    else:
        if not args.case:
            parser.error("--case is required for qualification-case")
        result = QualificationBatchRunner(workspace=workspace, root=output_root).run_case(
            args.case, allow_new_real_run=args.allow_new_real_run,
        )
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
