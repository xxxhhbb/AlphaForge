"""Offline executable demonstration of the AlphaForge research OS."""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

from ..data import DatasetRegistry, LABEL_FIELD, build_synthetic_market_fixture
from .service import AlphaForgeResearchOS
from .acceptance import evaluate_integrated_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--question", default="挖一下最近50天大动量的因子")
    parser.add_argument("--run-id", default="alphaforge-research-os-seed7")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--offline", action="store_true", help="explicitly document that no network/provider is used")
    parser.add_argument(
        "--artifact-root",
        default=".autobuild/veriquant/integration/research_os_demo",
    )
    args = parser.parse_args()
    fixture = build_synthetic_market_fixture(seed=args.seed, session_count=120)
    registry = DatasetRegistry()
    fixture.register(registry)
    decision_time = fixture.calendar[-1] + timedelta(hours=7)
    snapshot = registry.as_of_snapshot(
        fixture.dataset_id,
        decision_time=decision_time,
        fields=("close", "volume"),
    )
    labels = {
        f"{row.event_time.isoformat()}|{row.symbol}": row.value
        for row in registry.labels(
            fixture.dataset_id,
            label_field=LABEL_FIELD,
            evaluation_time=decision_time,
        )
        if row.value is not None
    }
    service = AlphaForgeResearchOS(artifact_root=Path(args.artifact_root))
    result = service.run(
        run_id=args.run_id,
        question=args.question,
        snapshot=snapshot,
        labels=labels,
        seed=args.seed,
    )
    replay = service.run(
        run_id=args.run_id,
        question=args.question,
        snapshot=snapshot,
        labels=labels,
        seed=args.seed,
    )
    acceptance = evaluate_integrated_run(
        first=result,
        replay=replay,
        package_root=Path(".autobuild/veriquant/parallel"),
    )
    acceptance_root = Path(args.artifact_root) / "acceptance"
    acceptance_root.mkdir(parents=True, exist_ok=True)
    (acceptance_root / "acceptance.json").write_text(acceptance.canonical_json() + "\n", encoding="utf-8")
    from ..research_os_acceptance import ResearchOSAcceptanceEvaluator
    (acceptance_root / "acceptance.md").write_text(
        ResearchOSAcceptanceEvaluator().render_markdown(acceptance), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "selected_research_groups": result.selected_group_ids,
                "candidate_count": result.candidate_count,
                "terminal_candidate_count": result.terminal_candidate_count,
                "tournament_status": result.tournament_status,
                "best_observed_candidate_id": result.best_observed_candidate_id,
                "report_hash": result.report.artifact_hash,
                "publication_receipt_hash": result.publication.artifact_hash,
                "acceptance_status": acceptance.status.value,
                "acceptance_hash": acceptance.artifact_hash,
                "report_path": str(Path(args.artifact_root) / "published" / args.run_id / "report.md"),
                "data_source": "DETERMINISTIC_OFFLINE_FIXTURE",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
