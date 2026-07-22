from __future__ import annotations

import argparse
import json
from pathlib import Path

from .harness import ProductStabilityHarness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AlphaForge product-spine stability acceptance.")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--dotenv", type=Path, default=Path("agent/.env"))
    args = parser.parse_args(argv)
    report = ProductStabilityHarness(
        output_root=args.output_root,
        data_root=args.data_root,
        dotenv_path=args.dotenv,
    ).run()
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.status == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
