"""Real configured-model U6 acceptance over the active frozen A-share data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_run_controller import ResearchRunLifecycle
from .product_service import ResearchOSV3ProductService


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default="挖一下最近50天的大动量因子")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--dotenv", type=Path, required=True)
    args = parser.parse_args(argv)

    active = FrozenValidationDataRepository(args.data_root / "data_active")
    base = FrozenValidationDataRepository(args.data_root / "data")
    service = ResearchOSV3ProductService(
        root=args.output_root,
        data_loader=lambda: active.load() or base.load(),
        dotenv_path=args.dotenv,
    )
    try:
        plan = service.create_plan({"question": args.question, "depth": "标准研究", "interactionStyle": "标准式"})
        confirmed = service.confirm_plan(str(plan["campaignId"]))
        started = service.start(str(confirmed["campaignId"]), seed=args.seed)
        terminal = service.control.wait(started.run_id, timeout=args.timeout)
        ok = (
            terminal.current_stage in {ResearchRunLifecycle.COMPLETED, ResearchRunLifecycle.PARTIAL}
            and terminal.report_hash is not None
            and terminal.official_evidence_hash is not None
        )
        result = service.load_result(started.run_id) if terminal.report_hash else None
        payload = {
            "status": "PASSED" if ok else "FAILED",
            "run_id": started.run_id,
            "current_stage": terminal.current_stage.value,
            "selected_specialists": terminal.selected_group_ids,
            "candidate_count": terminal.candidate_count,
            "official_evidence_hash": terminal.official_evidence_hash,
            "report_hash": terminal.report_hash,
            "manifest_hash": terminal.artifact_hash,
            "result_hash": result.artifact_hash if result else None,
            "source_label": "APPROVED_PROXY_DATA_VENDOR",
            "official_tushare": False,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 2
    finally:
        service.control.close()


if __name__ == "__main__":
    raise SystemExit(main())
