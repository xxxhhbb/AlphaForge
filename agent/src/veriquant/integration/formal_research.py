"""Run the formal AlphaForge research-report closure on approved proxy data."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..campaign_official_evidence import CampaignOfficialEvidenceBridge, CampaignOfficialEvidenceRequest
from ..campaign_real_data_binding import ApprovedProxyRealDataBinder, RealDataBindingRequest
from ..contracts import ContractValidationError, FailureCode
from ..contracts.base import canonicalize
from ..formal_report import FormalReportComposer, ResearchGroupSection, render_markdown
from ..formal_report_publication import FormalWorkbenchPublisher


SCHEMA_VERSION = "alphaforge.formal-research-run/v1"


def run_formal_research(
    *,
    question: str,
    output_root: str | Path,
    workbench_root: str | Path | None = None,
    seed: int = 7,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the current formal closure chain and return machine-readable output."""

    if not isinstance(question, str) or not question.strip():
        raise ContractValidationError(FailureCode.INVALID_VALUE, "question is required")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ContractValidationError(FailureCode.INVALID_VALUE, "seed must be a non-negative integer")
    stable_run_id = run_id or "formal-" + _hash({"question": question.strip(), "seed": seed})[7:23]
    _require_safe_id(stable_run_id, "run_id")
    root = Path(output_root).resolve() / stable_run_id
    root.mkdir(parents=True, exist_ok=True)
    campaign_hash = _hash({"question": question.strip(), "seed": seed, "profile": "approved-proxy-visible"})
    question_hash = _hash({"question": question.strip()})
    data_result = ApprovedProxyRealDataBinder().bind(
        RealDataBindingRequest(
            request_id=f"bind.{stable_run_id}",
            campaign_hash=campaign_hash,
            question_hash=question_hash,
            required_fields=("close", "volume"),
        )
    )
    if data_result.receipt is None:
        raise ContractValidationError(FailureCode.INCOMPLETE_RESULT, "formal run requires verified data receipt")
    evidence_result = CampaignOfficialEvidenceBridge().run(
        CampaignOfficialEvidenceRequest(
            request_id=f"official.{stable_run_id}",
            campaign_id=f"campaign.{stable_run_id}",
            run_id=stable_run_id,
            campaign_hash=campaign_hash,
            dataset_receipt=data_result.receipt,
            output_root=str(root / "official_evidence"),
            seed=seed,
        )
    )
    report = FormalReportComposer().compose(
        report_id=f"report.{stable_run_id}",
        question=question.strip(),
        mode_label="新 Alpha 探索",
        depth_label="标准研究",
        interaction_label="批处理式",
        campaign_outcome=evidence_result.campaign_outcome,
        dataset_receipt=data_result.receipt,
        research_groups=_research_groups(),
        candidate_expressions={
            "proxy-reversal-3d": "rank_cross_section(pct_change(close, 3))",
            "proxy-volume-change-3d": "zscore_cross_section(pct_change(volume, 3))",
        },
    )
    report_json = root / "formal_report.json"
    report_md = root / "formal_report.md"
    _atomic_json(report_json, report.to_dict())
    _atomic_text(report_md, render_markdown(report))
    publication_root = Path(workbench_root).resolve() if workbench_root else _repo_root() / ".autobuild/veriquant/integration/workbench_artifacts"
    publication = FormalWorkbenchPublisher().publish(
        root=publication_root,
        report=report,
        updated_at=data_result.receipt.decision_time.isoformat(),
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETED",
        "run_id": stable_run_id,
        "question": question.strip(),
        "data_binding_hash": data_result.artifact_hash,
        "dataset_receipt_hash": data_result.receipt.artifact_hash,
        "official_evidence_result_hash": evidence_result.artifact_hash,
        "research_outcome": evidence_result.campaign_outcome.research_outcome.value,
        "formal_report_hash": report.artifact_hash,
        "formal_report_json": str(report_json),
        "formal_report_markdown": str(report_md),
        "workbench_publication_hash": publication.artifact_hash,
        "workbench_root": str(publication_root),
        "limitation": "当前命令使用 approved proxy 可见证据基线候选；动态真实 LLM 候选接入需通过后续公共 runtime 集成。",
    }
    _atomic_json(root / "result.json", result)
    return result


def _research_groups() -> tuple[ResearchGroupSection, ...]:
    return (
        ResearchGroupSection(
            group_id="market_structure",
            group_label="市场结构研究小组",
            hypothesis="短期价格强弱与成交量变化可作为 A 股横截面候选信号的基线。",
            candidate_expression="rank_cross_section(pct_change(close, 3))",
            status="已提交候选并进入机器验证",
        ),
        ResearchGroupSection(
            group_id="meta_alpha",
            group_label="元阿尔法研究小组",
            hypothesis="成交量变化基线可作为动量类候选的对照。",
            candidate_expression="zscore_cross_section(pct_change(volume, 3))",
            status="已提交候选并进入机器验证",
        ),
    )


def _hash(value: Any) -> str:
    payload = json.dumps(canonicalize(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _require_safe_id(value: str, field_name: str) -> None:
    if not value or any(token in value for token in ("..", "/", "\\")) or len(value) > 96:
        raise ContractValidationError(FailureCode.INVALID_IDENTIFIER, f"{field_name} is not safe")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(canonicalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    _atomic_text(path, payload.rstrip("\n"))


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_name = handle.name
    try:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run AlphaForge formal research closure")
    parser.add_argument("--question", required=True)
    parser.add_argument("--output-root", type=Path, default=_repo_root() / ".autobuild/veriquant/formal_research")
    parser.add_argument("--workbench-root", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--run-id", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_formal_research(
            question=args.question,
            output_root=args.output_root,
            workbench_root=args.workbench_root,
            seed=args.seed,
            run_id=args.run_id,
        )
    except ContractValidationError as exc:
        print(json.dumps({"status": "FAILED", "failure_code": exc.code.value}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
