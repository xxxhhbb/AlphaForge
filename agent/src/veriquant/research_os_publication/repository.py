"""Filesystem publication repository for Research OS reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from veriquant.contracts import FailureCode
from veriquant.contracts.base import canonicalize, fail, require_hash
from veriquant.formal_report_v2 import FormalResearchReportV2

from .models import (
    LatestPublicationPointer,
    PublicationArtifactKind,
    PublicationArtifactRef,
    PublicationStatus,
    ResearchOSArtifactManifest,
    ResearchOSPublicationReceipt,
    assert_publication_payload_safe,
    require_safe_run_id,
)


MACHINE_REPORT_FILE = "machine_report.json"
MARKDOWN_REPORT_FILE = "report.md"
MANIFEST_FILE = "artifact_manifest.json"
RECEIPT_FILE = "publication_receipt.json"
LATEST_FILE = "latest.json"


class ResearchOSPublicationRepository:
    """Write and read redacted report artifacts without public routing."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def publish(
        self,
        *,
        report: FormalResearchReportV2,
        acceptance_report_hash: str,
        run_evidence_hash: str,
    ) -> ResearchOSPublicationReceipt:
        if not isinstance(report, FormalResearchReportV2):
            fail(FailureCode.INVALID_VALUE, "report must be FormalResearchReportV2")
        require_hash(acceptance_report_hash, "acceptance_report_hash")
        require_hash(run_evidence_hash, "run_evidence_hash")
        require_safe_run_id(report.run_id)

        run_dir = self._run_dir(report.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        machine_bytes = _canonical_json_bytes(report.to_dict())
        markdown_bytes = _render_chinese_markdown(report, acceptance_report_hash, run_evidence_hash).encode("utf-8")
        assert_publication_payload_safe(json.loads(machine_bytes.decode("utf-8")), "machine_report")
        assert_publication_payload_safe(markdown_bytes.decode("utf-8"), "markdown_report")

        manifest = ResearchOSArtifactManifest(
            run_id=report.run_id,
            report_hash=report.artifact_hash,
            acceptance_report_hash=acceptance_report_hash,
            run_evidence_hash=run_evidence_hash,
            artifacts={
                "machine_report": _artifact_ref(
                    PublicationArtifactKind.MACHINE_JSON,
                    MACHINE_REPORT_FILE,
                    machine_bytes,
                    "application/json",
                ),
                "chinese_markdown": _artifact_ref(
                    PublicationArtifactKind.CHINESE_MARKDOWN,
                    MARKDOWN_REPORT_FILE,
                    markdown_bytes,
                    "text/markdown",
                ),
            },
        )
        manifest_bytes = manifest.canonical_bytes()
        receipt = ResearchOSPublicationReceipt(
            run_id=report.run_id,
            status=PublicationStatus.PUBLISHED,
            report_hash=report.artifact_hash,
            acceptance_report_hash=acceptance_report_hash,
            run_evidence_hash=run_evidence_hash,
            manifest_hash=manifest.artifact_hash,
            artifact_hashes=tuple(
                sorted(
                    {
                        report.artifact_hash,
                        acceptance_report_hash,
                        run_evidence_hash,
                        manifest.artifact_hash,
                        manifest.artifacts["machine_report"].content_hash,
                        manifest.artifacts["chinese_markdown"].content_hash,
                    }
                )
            ),
            run_relative_root=report.run_id,
        )
        receipt_bytes = receipt.canonical_bytes()

        run_payloads = {
            run_dir / MACHINE_REPORT_FILE: machine_bytes,
            run_dir / MARKDOWN_REPORT_FILE: markdown_bytes,
            run_dir / MANIFEST_FILE: manifest_bytes,
            run_dir / RECEIPT_FILE: receipt_bytes,
        }
        self._assert_no_conflicts(run_payloads)
        for path, payload in run_payloads.items():
            _write_new_or_same(path, payload)

        pointer = LatestPublicationPointer(
            run_id=report.run_id,
            receipt_hash=receipt.artifact_hash,
            manifest_hash=manifest.artifact_hash,
            report_hash=report.artifact_hash,
        )
        _atomic_replace(self._safe_root_path(LATEST_FILE), pointer.canonical_bytes())
        return receipt

    def read_receipt(self, run_id: str) -> ResearchOSPublicationReceipt:
        path = self._run_dir(run_id) / RECEIPT_FILE
        return ResearchOSPublicationReceipt.from_payload(_read_json(path))

    def read_manifest(self, run_id: str) -> ResearchOSArtifactManifest:
        path = self._run_dir(run_id) / MANIFEST_FILE
        return ResearchOSArtifactManifest.from_payload(_read_json(path))

    def read_machine_report(self, run_id: str) -> Mapping[str, Any]:
        return _read_json(self._run_dir(run_id) / MACHINE_REPORT_FILE)

    def read_markdown(self, run_id: str) -> str:
        path = self._run_dir(run_id) / MARKDOWN_REPORT_FILE
        if not path.exists():
            fail(FailureCode.MISSING_REQUIRED_FIELD, "markdown report does not exist")
        return path.read_text(encoding="utf-8")

    def read_latest(self) -> LatestPublicationPointer:
        return LatestPublicationPointer.from_payload(_read_json(self._safe_root_path(LATEST_FILE)))

    def list_receipts(self) -> tuple[ResearchOSPublicationReceipt, ...]:
        if not self.root.exists():
            return ()
        receipts: list[ResearchOSPublicationReceipt] = []
        for item in sorted(self.root.iterdir(), key=lambda path: path.name):
            if not item.is_dir():
                continue
            try:
                require_safe_run_id(item.name)
            except Exception:
                continue
            receipt_path = item / RECEIPT_FILE
            if receipt_path.exists():
                receipts.append(ResearchOSPublicationReceipt.from_payload(_read_json(receipt_path)))
        return tuple(receipts)

    def _run_dir(self, run_id: str) -> Path:
        require_safe_run_id(run_id)
        root = self.root.resolve()
        target = (root / run_id).resolve()
        if target == root or root not in target.parents:
            fail(FailureCode.INVALID_IDENTIFIER, "run_id escapes publication root")
        return target

    def _safe_root_path(self, filename: str) -> Path:
        root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        target = (root / filename).resolve()
        if target.parent != root:
            fail(FailureCode.INVALID_IDENTIFIER, "publication pointer escapes root")
        return target

    @staticmethod
    def _assert_no_conflicts(payloads: Mapping[Path, bytes]) -> None:
        for path, payload in payloads.items():
            if path.exists() and path.read_bytes() != payload:
                fail(FailureCode.INVALID_STATE, "publication conflict: existing artifact content differs", path=str(path))


def _artifact_ref(
    kind: PublicationArtifactKind,
    relative_path: str,
    payload: bytes,
    content_type: str,
) -> PublicationArtifactRef:
    return PublicationArtifactRef(
        kind=kind,
        relative_path=relative_path,
        content_hash=_sha256(payload),
        content_type=content_type,
        byte_size=len(payload),
    )


def _render_chinese_markdown(
    report: FormalResearchReportV2,
    acceptance_report_hash: str,
    run_evidence_hash: str,
) -> str:
    lines = [
        "# AlphaForge（千因研究院）正式研究报告",
        "",
        "## 一页结论",
        "",
        f"- 研究编号：`{report.run_id}`",
        f"- 研究问题：{report.question}",
        f"- 机器结论：{report.executive_answer}",
        f"- 研究模式：{report.research_mode_label}",
        f"- 研究深度：{report.research_depth_label}",
        f"- 协作方式：{report.collaboration_label}",
        "",
        "## 数据版本与可研究性",
        "",
        f"- 数据来源标签：`{report.data_coverage.provider_label}`",
        f"- 可用交易日：{report.data_coverage.available_sessions} / {report.data_coverage.required_sessions}",
        f"- 可用股票数：{report.data_coverage.available_symbols} / {report.data_coverage.required_symbols}",
        f"- 观察窗口：{report.data_coverage.lookback_sessions} 个交易日",
        f"- 预测周期：{report.data_coverage.label_horizon_sessions} 个交易日",
        f"- 必需字段：{', '.join(report.data_coverage.required_fields) or '无'}",
        f"- 缺失必需字段：{', '.join(report.data_coverage.missing_required_fields) or '无'}",
        f"- 机器证据是否可启动：{'是' if report.researchability.official_evaluation_ready else '否'}",
        "",
        "## 被选择的专业研究小组",
        "",
    ]
    for group in report.selected_research_groups:
        lines.extend(
            [
                f"### {group.group_label}",
                f"- 假设：{group.hypothesis}",
                f"- 候选：{group.candidate_name}",
                f"- 受限表达式：`{group.candidate_expression}`",
                f"- 状态：{group.status}",
                "",
            ]
        )
    lines.extend(["## 候选与机器证据", ""])
    for candidate in report.candidates:
        lines.extend(
            [
                f"### {candidate.candidate_id}",
                f"- 来源小组：{candidate.group_label}",
                f"- 假设：{candidate.hypothesis}",
                f"- 受限表达式：`{candidate.expression}`",
                f"- 转译状态：{candidate.translation_status}",
                f"- 时间点检查：{candidate.pit_status}",
                f"- 机器证据：{candidate.evidence_status}",
                f"- 审查结论：{candidate.critic_status}",
                f"- 候选结果：`{candidate.outcome.value}`",
            ]
        )
        if candidate.official_metrics:
            for metric in candidate.official_metrics:
                lines.append(
                    f"- {metric.metric_name} [{metric.split}, {metric.policy_version}] = {metric.value:.6g}; 证据 `{metric.evidence_hash}`"
                )
        else:
            lines.append("- 正式指标：无；发布层不会补写或猜测指标。")
        for reason in candidate.failure_reasons:
            lines.append(f"- 原因：{reason}")
        for action in candidate.next_actions:
            lines.append(f"- 下一步：{action}")
        lines.append("")
    lines.extend(
        [
            "## 候选比较",
            "",
            report.comparison_summary,
            "",
            "## 限制与后续动作",
            "",
        ]
    )
    for limitation in report.limitations:
        lines.append(f"- {limitation}")
    for action in report.next_actions:
        lines.append(f"- 下一步：{action}")
    lines.extend(
        [
            "",
            "## 可追溯哈希",
            "",
            f"- 正式报告：`{report.artifact_hash}`",
            f"- 机械验收：`{acceptance_report_hash}`",
            f"- 运行证据：`{run_evidence_hash}`",
        ]
    )
    for artifact_hash in report.artifact_hashes:
        lines.append(f"- 上游产物：`{artifact_hash}`")
    markdown = "\n".join(lines).rstrip() + "\n"
    assert_publication_payload_safe(markdown, "markdown_report")
    return markdown


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_json(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        fail(FailureCode.MISSING_REQUIRED_FIELD, "publication artifact does not exist", path=str(path))
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_new_or_same(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() == payload:
            return
        fail(FailureCode.INVALID_STATE, "publication conflict: existing artifact content differs", path=str(path))
    _atomic_replace(path, payload)


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{_sha256(payload)[7:23]}.tmp")
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _sha256(payload: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(payload).hexdigest()

