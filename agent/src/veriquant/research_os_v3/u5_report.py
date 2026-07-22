"""Compose and publish the deterministic U5 Chinese report for a U4 run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..research_campaign import ResearchCampaign
from .multi_models import MultiSpecialistRunResult
from .reporting import ResearchOSV3ReportBackend
from .tools import FrozenResearchContext


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a U5 report from committed U4 DRAFT artifacts")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--publication-root", type=Path, required=True)
    args = parser.parse_args(argv)
    campaign = ResearchCampaign.from_payload(_read(args.artifact_root / "u4_campaign.json"))
    context = FrozenResearchContext.from_payload(_read(args.artifact_root / "u4_context.json"))
    smoke = _read(args.artifact_root / "u4_smoke_result.json")
    result = MultiSpecialistRunResult.from_payload(smoke["result"])
    report, receipt = ResearchOSV3ReportBackend(
        run_root=args.artifact_root,
        publication_root=args.publication_root,
    ).compose_and_publish(campaign=campaign, context=context, result=result)
    print(json.dumps({
        "candidate_count": report.candidate_count,
        "machine_evidence_status": report.machine_evidence_status,
        "outcome": report.outcome.value,
        "publication_root": receipt.publication_root,
        "receipt_hash": receipt.artifact_hash,
        "report_hash": report.artifact_hash,
        "run_id": report.run_id,
        "status": receipt.status,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
