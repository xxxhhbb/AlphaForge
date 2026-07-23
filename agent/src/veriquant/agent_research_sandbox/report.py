"""Deterministic customer report contracts for Agent research runs.

The composer translates already-bound machine facts into Chinese.  It never
calculates a metric and cannot upgrade an admission or promotion state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from .process_runner import canonical_hash


class ResearchConclusion(str, Enum):
    SUPPORTED = "SUPPORTED"
    CONDITIONALLY_SUPPORTED = "CONDITIONALLY_SUPPORTED"
    REJECTED = "REJECTED"
    REVISE = "REVISE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NEEDS_EXTERNAL_APPROVAL = "NEEDS_EXTERNAL_APPROVAL"
    NEGATIVE_RESEARCH_RESULT = "NEGATIVE_RESEARCH_RESULT"
    DATA_GAP = "DATA_GAP"
    PARTIAL_RESEARCH_RESULT = "PARTIAL_RESEARCH_RESULT"


_CONCLUSION_ZH = {
    ResearchConclusion.SUPPORTED: "机器证据支持该研究命题。",
    ResearchConclusion.CONDITIONALLY_SUPPORTED: "机器证据仅在明确条件下支持该研究命题。",
    ResearchConclusion.REJECTED: "当前机器证据不支持该研究命题。",
    ResearchConclusion.REVISE: "候选已经完成机器评价，但需要按审查意见修订后再验证。",
    ResearchConclusion.INSUFFICIENT_EVIDENCE: "实验已完成，但真实样本的统计功效不足，暂不能下结论。",
    ResearchConclusion.NEEDS_EXTERNAL_APPROVAL: "研究设计已完成，但继续评价需要用户批准新的数据字段、来源或权限。",
    ResearchConclusion.NEGATIVE_RESEARCH_RESULT: "本轮研究已完成，但没有候选通过统一审查或形成可支持的机器证据。",
    ResearchConclusion.DATA_GAP: "本轮已形成数据缺口报告；当前证据条件不足以完成官方评价。",
    ResearchConclusion.PARTIAL_RESEARCH_RESULT: "本轮保留了已完成的调查、审查和实验产物，但尚未形成完整官方结论。",
}

_FORBIDDEN = re.compile(
    r"(?i)(?:bearer\s+\S+|authorization\s*:|api[_-]?key\s*[:=]|token\s*[:=]|"
    r"sealed(?:_|\s)*(?:metric|data|value)|\btarget_weights?\b|\borders?\b|"
    r"\bbroker_credentials?\b)"
)


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if _FORBIDDEN.search(value):
        raise ValueError(f"{name} contains privileged or secret material")
    return value.strip()


def _items(values: Sequence[str], name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    result = tuple(_text(item, name) for item in values)
    if not allow_empty and not result:
        raise ValueError(f"{name} cannot be empty")
    return result


@dataclass(frozen=True, slots=True)
class BoundMetric:
    name: str
    display_value: str
    evidence_hash: str

    def __post_init__(self) -> None:
        _text(self.name, "metric name")
        _text(self.display_value, "metric value")
        if not isinstance(self.evidence_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.evidence_hash):
            raise ValueError("metric requires a bound evidence hash")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "display_value": self.display_value, "evidence_hash": self.evidence_hash}


@dataclass(frozen=True, slots=True)
class ResearchProcessNarrative:
    """Customer-safe reconstruction of one specialist's actual research path."""

    specialist: str
    status: str
    plan: str
    investigations: tuple[str, ...]
    attempts: tuple[str, ...]
    review_findings: tuple[str, ...]
    evidence_summary: tuple[str, ...]
    artifact_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.specialist, "process specialist")
        _text(self.status, "process status")
        _text(self.plan, "process plan")
        for name in ("investigations", "attempts", "review_findings", "evidence_summary"):
            _items(getattr(self, name), f"process {name}")
        if not self.artifact_hashes or any(
            not isinstance(item, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item)
            for item in self.artifact_hashes
        ):
            raise ValueError("process artifact_hashes must contain sha256 identities")

    def to_dict(self) -> dict[str, Any]:
        return {
            "specialist": self.specialist,
            "status": self.status,
            "plan": self.plan,
            "investigations": list(self.investigations),
            "attempts": list(self.attempts),
            "review_findings": list(self.review_findings),
            "evidence_summary": list(self.evidence_summary),
            "artifact_hashes": list(self.artifact_hashes),
        }


@dataclass(frozen=True, slots=True)
class CustomerResearchReport:
    question_id: str
    question_hash: str
    question: str
    conclusion: ResearchConclusion
    specialists: tuple[str, ...]
    research_routes: tuple[str, ...]
    data_description: tuple[str, ...]
    experiments: tuple[str, ...]
    review_summary: tuple[str, ...]
    metrics: tuple[BoundMetric, ...]
    robustness_checks: tuple[str, ...]
    supporting_evidence: tuple[str, ...]
    opposing_evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    next_steps: tuple[str, ...]
    artifact_hashes: tuple[str, ...]
    research_process: tuple[ResearchProcessNarrative, ...] = ()
    research_answer: Mapping[str, Any] | None = None
    report_type: str = "ALPHAFORGE_CUSTOMER_RESEARCH_REPORT"
    schema_version: str = "alphaforge.customer-research-report/v2"

    def __post_init__(self) -> None:
        _text(self.question_id, "question_id")
        _text(self.question, "question")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.question_hash):
            raise ValueError("question_hash must be sha256")
        if canonical_hash({"question": self.question}) != self.question_hash:
            raise ValueError("question_hash does not bind the question")
        for name in (
            "specialists", "research_routes", "data_description", "experiments",
            "review_summary", "robustness_checks", "limitations", "next_steps",
        ):
            _items(getattr(self, name), name)
        _items(self.supporting_evidence, "supporting_evidence", allow_empty=True)
        _items(self.opposing_evidence, "opposing_evidence", allow_empty=True)
        if not isinstance(self.metrics, tuple):
            raise ValueError("metrics must be a tuple")
        for metric in self.metrics:
            if not isinstance(metric, BoundMetric):
                raise ValueError("metrics must contain BoundMetric")
        if not isinstance(self.research_process, tuple) or any(
            not isinstance(item, ResearchProcessNarrative) for item in self.research_process
        ):
            raise ValueError("research_process must contain ResearchProcessNarrative")
        if self.schema_version == "alphaforge.customer-research-report/v3":
            if not isinstance(self.research_answer, Mapping):
                raise ValueError("v3 report requires a ResearchAnswer payload")
            answer = dict(self.research_answer)
            persisted = answer.pop("artifact_hash", None)
            if persisted is None or canonical_hash(answer) != persisted:
                raise ValueError("ResearchAnswer payload hash mismatch")
        if self.conclusion in {
            ResearchConclusion.SUPPORTED,
            ResearchConclusion.CONDITIONALLY_SUPPORTED,
            ResearchConclusion.REJECTED,
            ResearchConclusion.REVISE,
        } and not self.metrics:
            raise ValueError("machine conclusion requires bound metrics")
        if self.conclusion is ResearchConclusion.NEEDS_EXTERNAL_APPROVAL and not any(
            "批准" in item or "权限" in item or "数据源" in item for item in self.next_steps
        ):
            raise ValueError("external approval conclusion requires a concrete approval request")
        if not self.artifact_hashes or any(
            not isinstance(item, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item)
            for item in self.artifact_hashes
        ):
            raise ValueError("artifact_hashes must contain sha256 identities")
        serialized = json.dumps(self.to_dict(include_hash=False), ensure_ascii=False, sort_keys=True)
        if _FORBIDDEN.search(serialized):
            raise ValueError("report contains privileged or secret material")

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": self.schema_version,
            "report_type": self.report_type,
            "question_id": self.question_id,
            "question_hash": self.question_hash,
            "question": self.question,
            "conclusion": self.conclusion.value,
            "conclusion_zh": _CONCLUSION_ZH[self.conclusion],
            "specialists": list(self.specialists),
            "research_routes": list(self.research_routes),
            "data_description": list(self.data_description),
            "experiments": list(self.experiments),
            "review_summary": list(self.review_summary),
            "metrics": [item.to_dict() for item in self.metrics],
            "robustness_checks": list(self.robustness_checks),
            "supporting_evidence": list(self.supporting_evidence),
            "opposing_evidence": list(self.opposing_evidence),
            "limitations": list(self.limitations),
            "next_steps": list(self.next_steps),
            "artifact_hashes": list(self.artifact_hashes),
        }
        if self.schema_version != "alphaforge.customer-research-report/v1" or self.research_process:
            value["research_process"] = [item.to_dict() for item in self.research_process]
        if self.research_answer is not None:
            value["research_answer"] = dict(self.research_answer)
        if include_hash:
            value["report_hash"] = canonical_hash(value)
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CustomerResearchReport":
        """Rebuild a persisted report contract and verify its identity.

        Markdown is a deterministic customer view over this contract.  A
        delivery harness can therefore improve that view without rerunning the
        researcher or changing any machine evidence.
        """

        report = cls(
            question_id=str(value["question_id"]),
            question_hash=str(value["question_hash"]),
            question=str(value["question"]),
            conclusion=ResearchConclusion(str(value["conclusion"])),
            specialists=tuple(str(item) for item in value["specialists"]),
            research_routes=tuple(str(item) for item in value["research_routes"]),
            data_description=tuple(str(item) for item in value["data_description"]),
            experiments=tuple(str(item) for item in value["experiments"]),
            review_summary=tuple(str(item) for item in value["review_summary"]),
            metrics=tuple(
                BoundMetric(
                    name=str(item["name"]),
                    display_value=str(item["display_value"]),
                    evidence_hash=str(item["evidence_hash"]),
                )
                for item in value["metrics"]
            ),
            robustness_checks=tuple(str(item) for item in value["robustness_checks"]),
            supporting_evidence=tuple(str(item) for item in value["supporting_evidence"]),
            opposing_evidence=tuple(str(item) for item in value["opposing_evidence"]),
            limitations=tuple(str(item) for item in value["limitations"]),
            next_steps=tuple(str(item) for item in value["next_steps"]),
            artifact_hashes=tuple(str(item) for item in value["artifact_hashes"]),
            research_process=tuple(
                ResearchProcessNarrative(
                    specialist=str(item["specialist"]),
                    status=str(item["status"]),
                    plan=str(item["plan"]),
                    investigations=tuple(str(entry) for entry in item["investigations"]),
                    attempts=tuple(str(entry) for entry in item["attempts"]),
                    review_findings=tuple(str(entry) for entry in item["review_findings"]),
                    evidence_summary=tuple(str(entry) for entry in item["evidence_summary"]),
                    artifact_hashes=tuple(str(entry) for entry in item["artifact_hashes"]),
                )
                for item in value.get("research_process", ())
            ),
            research_answer=(
                None if value.get("research_answer") is None
                else dict(value["research_answer"])
            ),
            report_type=str(value.get("report_type", "ALPHAFORGE_CUSTOMER_RESEARCH_REPORT")),
            schema_version=str(value.get("schema_version", "alphaforge.customer-research-report/v1")),
        )
        persisted_hash = value.get("report_hash")
        if persisted_hash is not None and persisted_hash != report.artifact_hash:
            raise ValueError("persisted report_hash does not bind the report contract")
        return report

    @property
    def artifact_hash(self) -> str:
        return self.to_dict()["report_hash"]


class ChineseResearchReportComposer:
    """Render a stable, professional report from machine-bound facts."""

    def render(self, report: CustomerResearchReport) -> str:
        if report.research_answer is not None:
            return self._render_v3(report)
        evidence_map = self._evidence_map(report.metrics)
        sections = [
            "# AlphaForge（千因研究院）研究报告",
            "",
            "## 一句话结论",
            "",
            self._headline_conclusion(report),
            "",
            self._decision_sentence(report),
            "",
            "## 研究问题",
            "",
            report.question,
            "",
            "## 先看重点",
            "",
        ]
        for item in self._plain_key_points(report):
            sections.append(f"- {item}")

        sections.extend(["", "## 本轮研究是怎样完成的", ""])
        sections.append(
            "系统没有让一个模型直接给答案，而是把问题交给不同专业研究组独立思考。"
            "每组先提出猜想，再查看获准数据、编写实验、接受代码审查并在隔离环境中运行，"
            "最后由机器统一复核结果。"
        )
        if report.research_process:
            for index, process in enumerate(report.research_process, start=1):
                sections.extend([
                    "", f"### {index}. {process.specialist}", "",
                    f"**本组任务**：{self._specialist_goal(process.specialist)}", "",
                    "**研究猜想与方法**", "", self._plain_plan(process.specialist, process.plan), "",
                    f"**完成情况**：{self._plain_status(process.status)}",
                ])
                self._append_compact_list(
                    sections, "调查了什么、实际尝试、审查与修订", self._plain_attempts(process)
                )
        else:
            self._append_list(sections, "参与的专业研究组", report.specialists)
            self._append_list(sections, "Agent 的研究路线", report.research_routes)
            self._append_list(sections, "实际执行的实验", report.experiments)

        sections.extend(["", "## 研究发现如何回答原问题", ""])
        score_findings = self._score_findings(report)
        findings = self._client_findings(report.metrics)
        if score_findings:
            sections.extend(score_findings)
        elif findings:
            sections.extend([
                "下表只保留最能回答问题的结果。这里的“统计线索”表示样本中观察到了关系，"
                "不表示已经证明可以赚钱。p 值越小，说明该关系越不像随机波动；它仍不是收益率。",
                "",
                "| 发现 | 跨组表现 | 最小 p 值 | 通俗解读 | 当前判断 |",
                "|---|---|---:|---|---|",
                *findings,
            ])
        elif report.metrics:
            sections.append(
                "本轮生成了机器指标，但当前输出合同不足以把这些数字解释成对原问题的直接回答；"
                "完整指标保留在附录，不能用指标清单冒充研究结论。"
            )
        else:
            sections.append("本结论没有可冒充的官方指标；具体原因见“没有完成什么”和下一步建议。")

        sections.extend(["", "## 原问题回答到了什么程度", ""])
        sections.extend(["| 需要回答的部分 | 本轮实际答案 | 关键依据 | 判断 |", "|---|---|---|---|"])
        sections.extend(self._coverage_rows(report))

        sections.extend(["", "## 这份结果应该怎么用", ""])
        sections.extend(self._usage_guidance(report))
        self._append_list(sections, "下一轮具体要做什么", self._specific_next_steps(report))

        sections.extend(["", "## 数据与可信度", ""])
        for item in self._plain_data_summary(report):
            sections.append(f"- {item}")
        sections.extend([
            "- 所有研究代码均经过机械安全检查，并在无网络隔离环境中运行。",
            "- 报告中的数字来自机器结果表；研究模型只能解释，不能改写这些数字。",
            "",
            "<details>",
            "<summary><strong>展开完整机器指标与方法复核信息</strong></summary>",
        ])

        labels = self._experiment_labels(report)
        self._append_list(
            sections, "代码、方法与反证审查",
            tuple(self._customer_label_text(item, labels) for item in report.review_summary),
        )
        self._append_list(sections, "稳健性与反证", report.robustness_checks)
        self._append_list(
            sections, "反对证据", self._customer_evidence_text(report.opposing_evidence),
            empty="未发现可用文字描述的决定性反对证据。",
        )
        self._append_list(
            sections, "数据和方法限制",
            tuple(self._customer_label_text(item, labels) for item in report.limitations),
        )

        if report.metrics:
            sections.extend(["", "## 完整机器指标", ""])
            for index, (experiment, metrics) in enumerate(self._group_metrics(report.metrics), start=1):
                label = labels.get(self._raw_experiment_id(experiment), f"实验 {index}")
                sections.extend([
                    f"### {self._escape(label)}", "",
                    "| 指标 | 结果 |", "|---|---:|",
                ])
                sections.extend(
                    f"| {self._escape(self._pretty_metric(name))} | {self._escape(item.display_value)} |"
                    for name, item in metrics
                )

        sections.extend([
            "", "## 复现与关联产物", "",
            f"- 报告合同：`{report.schema_version}`",
            "- 机器收据：数据身份、研究源码、审查、隔离运行和指标收据与报告 JSON 一同保存。",
            "- 复现要求：使用相同冻结输入、研究源码、随机种子与 Evidence 产物重放；不得由报告文字改写机器结论。",
            "", "## 技术与权限说明", "",
            "- Agent 只生成研究草案与代码；机器指标由独立 Evidence Service 计算。",
            "- 标为机器复核研究证据的结果尚未形成完整官方结论。",
            "",
            "</details>",
        ])
        return "\n".join(sections) + "\n"

    def _render_v3(self, report: CustomerResearchReport) -> str:
        """Render only explicit answer semantics; never infer an answer from names."""

        answer = dict(report.research_answer or {})
        candidates = {
            str(item["candidate_id"]): item
            for item in answer.get("candidate_definitions", ())
        }
        decisions = {
            str(item["candidate_id"]): item
            for item in answer.get("candidate_decisions", ())
        }
        lines = [
            "# AlphaForge（千因研究院）研究报告",
            "",
            "## 一句话答案",
            "",
            str(answer["direct_answer"]),
            "",
            "## 最终判断",
            "",
            f"- **回答状态**：{self._answer_status_zh(str(answer['answer_status']))}",
            f"- **研究结论**：{self._answer_verdict_zh(str(answer['research_verdict']))}",
            f"- **验证进度**：{self._answer_authority_zh(str(answer['evidence_authority']))}",
        ]
        if answer["evidence_authority"] == "AUXILIARY_ONLY" or (
            answer["evidence_authority"] == "NONE" and candidates
        ):
            lines.append("- **权限说明**：当前属于辅助研究，尚未形成完整官方结论；已完成产物仍可复核，但不能冒充正式答案。")
            if answer["research_verdict"] in {"SUPPORTED", "CONDITIONALLY_SUPPORTED"}:
                lines.append("- **研究价值**：当前线索值得继续验证，但证据权限不会因此自动升级。")
        if answer["research_verdict"] == "DATA_UNAVAILABLE":
            lines.append("- **结果类型**：本轮形成数据缺口报告，而不是伪造研究成功。")
        if answer["answer_status"] == "UNANSWERABLE_WITH_REASON" and candidates:
            lines.append("- **候选状态**：没有候选通过统一审查或形成可解释的机器指标。")
        lines.extend([
            "",
            "## 用户研究问题",
            "",
            report.question,
            "",
            "## 候选结论",
            "",
        ])
        if candidates:
            for candidate_id in sorted(candidates):
                candidate = candidates[candidate_id]
                decision = decisions[candidate_id]
                role = str(candidate.get("result_role") or "ALPHA_SIGNAL")
                definition_label = "Alpha 定义（实际计算方式）" if role == "ALPHA_SIGNAL" else "研究分析定义"
                lines.extend([
                    f"### {candidate['display_name']}",
                    "",
                    f"- **这项结果是什么**：{self._result_role_zh(role)}",
                    f"- **处置**：{self._candidate_disposition_zh(str(decision['disposition']))}",
                ])
                if candidate.get("implementation_summary"):
                    lines.append(
                        f"- **实际计算公式与方向**：{candidate['implementation_summary']}"
                    )
                if candidate.get("input_fields"):
                    lines.append(
                        "- **实际输入字段**：" + "、".join(candidate["input_fields"])
                    )
                if candidate.get("design_warnings"):
                    lines.append(
                        "- **设计审查意见**："
                        + "；".join(self._reason_zh(str(item)) for item in candidate["design_warnings"])
                    )
                if candidate.get("design_advisories"):
                    lines.append(
                        "- **稳健性提示**："
                        + "；".join(self._reason_zh(str(item)) for item in candidate["design_advisories"])
                    )
                if candidate.get("revision_of_source_hash"):
                    reasons = candidate.get("revision_reason_codes") or ()
                    lines.append(
                        "- **本轮修订**：该公式由上一版候选自动修订而来"
                        + (
                            "；修订原因："
                            + "；".join(self._reason_zh(str(item)) for item in reasons)
                            if reasons else ""
                        )
                        + "。"
                    )
                lines.extend([
                    f"- **{definition_label}**：{candidate['method_summary']}",
                    f"- **要验证的假设**：{candidate['hypothesis']}",
                    f"- **结果类型**：{self._output_kind_zh(str(candidate['output_kind']))}",
                    f"- **主要原因**：{'、'.join(self._reason_zh(str(item)) for item in decision['reason_codes'])}",
                    "",
                ])
        else:
            lines.extend(["本轮没有形成可进入机器评价的候选；具体原因见未完成部分。", ""])

        lines.extend(["## 原问题逐项回答", ""])
        for index, part in enumerate(answer.get("question_parts", ()), start=1):
            question_text = part.get("question_text")
            if question_text:
                lines.extend([f"### {index}. {question_text}", ""])
            lines.extend([
                f"**答案**：{part['direct_answer']}",
                "",
                f"- **判断**：{part['judgment']}",
                f"- **回答状态**：{self._answer_status_zh(str(part['answer_status']))}",
                f"- **验证进度**：{self._answer_authority_zh(str(part['evidence_authority']))}",
            ])
            support = tuple(str(item["text"]) for item in part.get("supporting_facts", ()))
            oppose = tuple(str(item["text"]) for item in part.get("opposing_facts", ()))
            self._append_list(lines, "支持依据", support, empty="没有可登记的支持证据。")
            self._append_list(lines, "反对或修订依据", oppose, empty="没有可登记的反对证据。")
            if part.get("unanswered_reason"):
                lines.append(f"- **未回答原因**：{part['unanswered_reason']}")
            if part.get("recovery_requirement"):
                lines.append(f"- **恢复条件**：{part['recovery_requirement']}")
            lines.append("")

        lines.extend(["## 机器指标与证据", ""])
        fact_count = sum(
            len(part.get("supporting_facts", ())) + len(part.get("opposing_facts", ()))
            for part in answer.get("question_parts", ())
        )
        if fact_count:
            lines.append(
                f"本轮共有 {fact_count} 条机器指标事实进入逐项回答；其 Evidence 身份保存在机器 JSON 中。"
            )
        else:
            lines.append("本轮没有可用于回答原问题的机器指标；系统没有用空指标冒充研究结论。")
        lines.append("")

        if report.research_process:
            lines.extend(["## 研究过程", ""])
            for index, process in enumerate(report.research_process, start=1):
                lines.extend([
                    f"### {index}. {process.specialist}", "",
                    f"- **完成状态**：{self._plain_status(process.status)}",
                    f"- **研究思路**：{process.plan}",
                ])
                self._append_list(lines, "实际调查与实验", self._plain_attempts(process))

        lines.extend(["## 冲突、限制与未完成部分", ""])
        conflicts = tuple(self._conflict_zh(str(item)) for item in answer.get("conflicts", ()))
        candidate_labels = {
            candidate_id: str(item["display_name"])
            for candidate_id, item in candidates.items()
        }
        self._append_list(lines, "研究组或指标冲突", conflicts, empty="未检测到需要披露的跨组冲突。")
        self._append_list(lines, "未测试范围", tuple(answer.get("untested_scope", ())), empty="所有登记的回答要求均已进入评价。")
        self._append_list(
            lines,
            "限制",
            tuple(
                self._customer_label_text(str(item), candidate_labels)
                for item in answer.get("limitations", ())
            ),
            empty="没有额外登记限制。",
        )
        lines.extend([
            "", "## 下一项具体实验", "",
            str(answer["next_experiment"]),
            "",
            "## 数据、复现与权限说明", "",
            "- 研究计划、候选定义、源码审查、隔离运行、标准结果表、Evidence、答案和报告均已分别保存。",
            "- Agent 只生成研究草案与代码；研究结论由独立机器 Evidence 和答案装配器生成。",
            "- 报告中的数字来自绑定的机器 Evidence；研究模型不能修改指标、答案状态或候选处置。",
            "- 技术哈希保存在机器 JSON 中，客户正文不展示内部标识。",
            "- 本报告不构成 Promotion、组合、风险审批或交易指令。",
        ])
        return "\n".join(lines) + "\n"

    @staticmethod
    def _answer_status_zh(value: str) -> str:
        return {
            "ANSWERED": "已回答",
            "CONDITIONALLY_ANSWERED": "有条件回答",
            "NEGATIVE_ANSWER": "已形成明确的负向答案",
            "PARTIALLY_ANSWERED": "部分回答",
            "UNANSWERABLE_WITH_REASON": "当前不可回答，原因与恢复条件已明确",
        }.get(value, value)

    @staticmethod
    def _answer_verdict_zh(value: str) -> str:
        return {
            "SUPPORTED": "机器证据支持",
            "CONDITIONALLY_SUPPORTED": "机器证据在明确条件下支持",
            "REJECTED": "当前机器证据不支持",
            "INSUFFICIENT_EVIDENCE": "证据不足",
            "DATA_UNAVAILABLE": "数据或评价条件不可用",
        }.get(value, value)

    @staticmethod
    def _answer_authority_zh(value: str) -> str:
        return {
            "NONE": "尚未形成可评价的机器结果",
            "AUXILIARY_ONLY": "已完成实验与机器一致性复核，尚待独立评价",
            "OFFICIAL_EVIDENCE": "已完成独立机器评价",
            "MIXED": "主要结果已完成独立机器评价；部分条件分析仍待补充复核",
        }.get(value, value)

    @staticmethod
    def _candidate_disposition_zh(value: str) -> str:
        return {
            "RETAIN": "保留",
            "RETAIN_WITH_CONDITIONS": "有条件保留",
            "REVISE": "修订后重测",
            "REJECT": "拒绝",
            "NOT_EVALUATED": "未完成评价",
        }.get(value, value)

    @staticmethod
    def _output_kind_zh(value: str) -> str:
        return {
            "ScoreTable": "横截面评分评价",
            "RegressionTable": "控制变量回归评价",
            "RegimeComparisonTable": "市场状态比较",
            "EventTable": "事件研究",
            "DiagnosticTable": "诊断研究",
            "DatasetAssessmentTable": "数据集适用性评价",
        }.get(value, value)

    @staticmethod
    def _result_role_zh(value: str) -> str:
        return {
            "ALPHA_SIGNAL": "可计算的横截面 Alpha 信号",
            "STATISTICAL_TEST": "控制变量后的统计检验，不是独立 Alpha",
            "CONDITION_ANALYSIS": "市场状态条件分析，用于限定 Alpha 的适用环境",
            "EVENT_STUDY": "事件效应研究",
            "DIAGNOSTIC": "根因诊断，不直接生成 Alpha",
            "DATA_ASSESSMENT": "数据适用性评估，不直接生成 Alpha",
        }.get(value, value)

    @staticmethod
    def _reason_zh(value: str) -> str:
        return {
            "MACHINE_EVIDENCE_SUPPORTS": "机器 Evidence 支持",
            "CONDITIONAL_MACHINE_SUPPORT": "存在机器支持，但仍有冲突或尚未完成的验证",
            "CANDIDATE_DEFINITION_INCOMPLETE": "候选没有完整说明输入字段、计算步骤和分数方向，补齐定义后重测",
            "CROSS_SECTION_CONSTANT_ADDITIVE_TERM": (
                "市场统一变量被直接作为横截面加法项；它不会改变同日股票排序，"
                "应改为市场状态分组或与个股特征交互后重测"
            ),
            "HIGH_FEATURE_CORRELATION": "输入特征高度相关；不阻断研究，但需检查冗余和结果稳定性",
            "MACHINE_EVIDENCE_REJECTS": "机器 Evidence 不支持",
            "MACHINE_REVIEW_REQUIRES_REVISION": "机器审查要求修订",
            "NO_MACHINE_VERDICT": "尚未形成机器结论",
        }.get(value, value)

    @staticmethod
    def _conflict_zh(value: str) -> str:
        if value.startswith("METRIC_DIRECTION_CONFLICT:"):
            return "不同研究路线对同一指标给出相反方向：" + value.split(":", 1)[1]
        return {
            "SPECIALIST_VERDICT_CONFLICT": "不同专业研究组对候选是否成立存在分歧。",
            "EVIDENCE_AUTHORITY_MIXED": "本轮同时包含官方和辅助 Evidence，结论按较低权限解释。",
        }.get(value, value)

    @staticmethod
    def _decision_sentence(report: CustomerResearchReport) -> str:
        if report.conclusion is ResearchConclusion.PARTIAL_RESEARCH_RESULT and report.metrics:
            return "**研究处置**：已经形成可解释的机器复核线索，建议进入下一轮严格验证；当前不据此晋级。"
        if report.conclusion in {ResearchConclusion.SUPPORTED, ResearchConclusion.CONDITIONALLY_SUPPORTED}:
            return "**研究处置**：保留候选，并按机器结论进入下一检查点。"
        if report.conclusion in {ResearchConclusion.REJECTED, ResearchConclusion.NEGATIVE_RESEARCH_RESULT}:
            return "**研究处置**：本轮不保留为有效候选；根据反对证据重新设计假设。"
        return "**研究处置**：保留已完成产物，先解决报告列出的缺口再继续。"

    def _plain_key_points(self, report: CustomerResearchReport) -> tuple[str, ...]:
        verdicts = tuple(self._experiment_verdicts(report).values())
        if any(value == "VALIDATED_RESEARCH" for value in verdicts):
            points = ["至少一条研究路线通过了可见机器验证，但仍需结合其他路线的反对证据决定是否修订。"]
            if any(value in {"REVISE", "REJECT", "REJECTED"} for value in verdicts):
                points.append("不同研究路线给出了冲突结论；报告不会用最好的单个指标掩盖这种分歧。")
            points.append("下文会分别说明预测关系、成本后表现、稳定性和增量性，而不是只罗列指标。")
            return tuple(points)
        records = self._aggregate_regression(report.metrics)
        ordered = sorted(
            (item for item in records.items() if item[1]["significant"]),
            key=lambda item: (
                -len(item[1]["significant"]),
                min(value[1] for value in item[1]["significant"]),
                item[0],
            ),
        )
        points: list[str] = []
        if ordered:
            leaders = "、".join(self._pretty_term(term) for term, _ in ordered[:3])
            points.append(f"最值得继续跟踪的变量是：{leaders}。它们在本次样本中出现了较明确的统计关系。")
        else:
            points.append("本轮没有发现足够稳定的正向线索，但已经完成了可复核的负向研究。")
        completed_contracts = sorted({
            self._split_metric(item.name)[0] for item in report.metrics
        })
        if completed_contracts:
            points.append(
                f"本轮已形成 {len(completed_contracts)} 组独立机器结果；"
                "具体完成项、未完成项和适用边界以本报告的覆盖表为准。"
            )
        else:
            points.append("本轮保留了研究计划、调查和失败诊断，但没有可用于机器判断的标准结果表。")
        points.append(
            "因此目前最合理的动作是保留候选方向并进入下一轮验证，而不是直接用于选股或交易。"
        )
        return tuple(points)

    @staticmethod
    def _specialist_goal(specialist: str) -> str:
        return {
            "基本面研究组": "判断估值和企业盈利质量是否能解释未来收益。",
            "市场行为与交易可行性研究组": "判断成交活跃度、流动性和市场环境是否带来可交易线索。",
            "组合增量研究组": "判断新线索在控制已有因子后是否仍然提供额外信息。",
            "宏观状态研究组": "判断同一信号在不同宏观和市场状态下是否稳定。",
            "预期与事件研究组": "判断公告、预期变化或事件发生后是否存在延迟反应。",
            "另类数据研究组": "判断新闻、关注度等非传统信息是否提供额外解释力。",
        }.get(specialist, "从本组专业视角独立检查用户问题。")

    @staticmethod
    def _plain_plan(specialist: str, plan: str) -> str:
        del specialist
        return plan.strip()

    @staticmethod
    def _customer_evidence_text(values: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            value for value in values
            if not re.search(r"sha256:[0-9a-f]{64}", value, flags=re.IGNORECASE)
        )

    @staticmethod
    def _plain_status(status: str) -> str:
        return {
            "COMPLETED": "本组已完成计划、数据检查、代码审查和隔离实验。",
            "PARTIAL": "本组完成了部分研究，未完成部分已在报告中明确列出。",
            "BLOCKED": "本组因数据或能力缺口未能完成实验，但保留了原因和已完成工作。",
            "FAILED": "本组遇到基础设施故障，不能把本次运行当作研究结论。",
        }.get(status, status)

    @staticmethod
    def _plain_attempts(process: ResearchProcessNarrative) -> tuple[str, ...]:
        attempted = sum("提交实验" in item for item in process.attempts)
        return (
            "先核对获准数据是否包含研究所需变量和时间信息。",
            f"随后设计并提交了 {attempted} 个可复核实验。" if attempted else "本组未进入实验执行阶段。",
            "实验代码接受了机械安全审查，并在无网络隔离环境中运行。",
            "运行结果被转换成标准表，再交给独立程序计算统计指标。",
        )

    def _aggregate_regression(
        self, metrics: Sequence[BoundMetric]
    ) -> dict[str, dict[str, list[tuple[float, float, str]]]]:
        grouped: dict[tuple[str, str], dict[str, BoundMetric]] = {}
        for item in metrics:
            experiment, name = self._split_metric(item.name)
            for prefix in ("coefficient.", "p_value_normal."):
                if name.startswith(prefix):
                    term = name[len(prefix):]
                    if term.casefold() not in {"intercept", "const"}:
                        grouped.setdefault((experiment, term), {})[prefix[:-1]] = item
                    break
        result: dict[str, dict[str, list[tuple[float, float, str]]]] = {}
        for (experiment, term), values in grouped.items():
            coefficient = values.get("coefficient")
            p_value = values.get("p_value_normal")
            if coefficient is None or p_value is None:
                continue
            try:
                row = (float(coefficient.display_value), float(p_value.display_value), experiment)
            except ValueError:
                continue
            bucket = result.setdefault(term, {"all": [], "significant": []})
            bucket["all"].append(row)
            if row[0] != 0 and row[1] <= 0.05:
                bucket["significant"].append(row)
        return result

    def _score_findings(self, report: CustomerResearchReport) -> list[str]:
        """Explain ScoreTable evidence as answers, never as an identifier dump."""
        labels = self._experiment_labels(report)
        verdicts = self._experiment_verdicts(report)
        blocks: list[str] = []
        score_groups = []
        for experiment, metrics in self._group_metrics(report.metrics):
            values = self._metric_values(metrics)
            if not any(key in values for key in ("ic_mean", "rank_ic", "net_return", "icir")):
                continue
            score_groups.append((experiment, metrics, values))
        if not score_groups:
            return blocks
        blocks.append(
            "机器不是按单个漂亮数字挑选候选，而是同时检查预测方向、成本后表现、"
            "跨时期/状态稳定性以及相对已登记因子的增量性。下面按研究路线解释结果。"
        )
        for index, (experiment, _metrics, values) in enumerate(score_groups, start=1):
            raw_id = self._raw_experiment_id(experiment)
            label = labels.get(raw_id, f"候选 {index}")
            verdict = verdicts.get(raw_id, "")
            blocks.extend(["", f"### {self._escape(label)}", ""])

            ic_mean = self._number(values.get("ic_mean"))
            rank_ic = self._number(values.get("rank_ic"))
            pearson_ic = self._number(values.get("pearson_ic"))
            direction_values = [value for value in (ic_mean, rank_ic, pearson_ic) if value is not None and value != 0]
            if direction_values and all(value > 0 for value in direction_values):
                direction_answer = "各项 IC 方向一致为正，候选评分较高的股票随后收益整体更高。"
            elif direction_values and all(value < 0 for value in direction_values):
                direction_answer = "各项 IC 方向一致为负，原始候选方向与预期相反。"
            elif direction_values:
                direction_answer = "不同 IC 口径方向冲突，说明候选排序关系不稳定，不能只引用其中一个正数。"
            else:
                direction_answer = "现有 IC 没有提供可识别方向。"
            blocks.append(f"**对预测能力的回答**：{direction_answer}")
            blocks.append(
                "- **预测证据**："
                + self._joined_metrics(values, (
                    ("ic_mean", "IC 均值"), ("rank_ic", "RankIC"),
                    ("pearson_ic", "Pearson IC"), ("icir", "ICIR"),
                ))
                + "。IC 衡量横截面评分与未来收益的关系；ICIR 衡量这种关系相对自身波动是否稳定。"
            )

            net_return = self._number(values.get("net_return"))
            if net_return is None:
                cost_answer = "尚未形成成本后收益，不能判断可交易性。"
            elif net_return > 0:
                cost_answer = "扣除成本后仍为正（已计入本次价差和冲击成本），但这不等同于已经可以投入交易。"
            elif net_return < 0:
                cost_answer = "扣除成本后为负，当前形式不具备可交易支持。"
            else:
                cost_answer = "扣除成本后没有剩余收益。"
            blocks.append(f"- **成本与交易性**：{cost_answer} " + self._joined_metrics(values, (
                ("gross_return", "成本前收益"), ("net_return", "成本后收益"),
                ("turnover", "换手率"), ("max_drawdown", "最大回撤"),
            )) + "。")

            sub_fraction = self._number(values.get("subperiod_positive_fraction"))
            regime_fraction = self._number(values.get("regime_positive_fraction"))
            if sub_fraction is not None and sub_fraction < 1:
                stability = "至少有部分子时期没有保持正向，时间稳定性尚未完全成立。"
            elif regime_fraction is not None and regime_fraction < 1:
                stability = "至少有部分市场状态没有保持正向，状态稳定性尚未完全成立。"
            elif sub_fraction == 1 and regime_fraction == 1:
                stability = "在当前划分的子时期和市场状态中方向均保持为正。"
            else:
                stability = "当前标准表没有给出足够的分期或分状态稳定性信息。"
            blocks.append(f"- **稳定性**：{stability} " + self._joined_metrics(values, (
                ("subperiod_min_ic", "最弱子时期 IC"),
                ("subperiod_positive_fraction", "子时期正向比例"),
                ("regime_min_ic", "最弱市场状态 IC"),
                ("regime_positive_fraction", "市场状态正向比例"),
            )) + "。")

            blocks.append("- **相对已有因子的增量性**：" + self._joined_metrics(values, (
                ("incremental_ic_mean", "增量 IC 均值"),
                ("max_registered_abs_correlation", "与已登记因子最高绝对相关性"),
            )) + "。相关性记录为 0 时仍需确认目标基准因子确实进入本次比较，不能把零值自动解释为完全独立。")
            if verdict:
                blocks.append(f"- **机器判断**：{self._plain_verdict(verdict)}")
        return blocks

    @staticmethod
    def _metric_values(metrics: Sequence[tuple[str, BoundMetric]]) -> dict[str, str]:
        reverse = {
            "成本前收益": "gross_return", "IC 均值": "ic_mean", "IC 标准差": "ic_std",
            "ICIR": "icir", "增量 IC 均值": "incremental_ic_mean",
            "线性冲击成本": "linear_impact_cost", "最大回撤": "max_drawdown",
            "与已登记因子最高相关性": "max_registered_abs_correlation",
            "成本后收益": "net_return", "有效观测数": "observation_count",
            "Pearson IC": "pearson_ic", "评价期数": "period_count",
            "分位数组合价差": "quantile_spread", "RankIC": "rank_ic",
            "最弱市场状态 IC": "regime_min_ic", "状态正向比例": "regime_positive_fraction",
            "价差成本": "spread_cost", "最弱子时期 IC": "subperiod_min_ic",
            "子时期正向比例": "subperiod_positive_fraction", "换手率": "turnover",
        }
        return {reverse.get(name, name): metric.display_value for name, metric in metrics}

    @staticmethod
    def _number(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _joined_metrics(values: Mapping[str, str], fields: Sequence[tuple[str, str]]) -> str:
        rendered = [f"{label} {values[key]}" for key, label in fields if key in values]
        return "；".join(rendered) if rendered else "本轮未生成对应指标"

    def _experiment_labels(self, report: CustomerResearchReport) -> dict[str, str]:
        result: dict[str, str] = {}
        for process in report.research_process:
            experiment_ids = []
            for attempt in process.attempts:
                match = re.search(r"提交实验\s+([^：]+)：", attempt)
                if match:
                    experiment_ids.append(match.group(1).strip())
            for index, experiment_id in enumerate(dict.fromkeys(experiment_ids), start=1):
                suffix = f" · 修订实验 {index}" if len(set(experiment_ids)) > 1 else " · 候选"
                result[experiment_id] = process.specialist + suffix
        return result

    @staticmethod
    def _experiment_verdicts(report: CustomerResearchReport) -> dict[str, str]:
        result: dict[str, str] = {}
        for process in report.research_process:
            for attempt in process.attempts:
                match = re.search(r"提交实验\s+([^：]+)：[^，。]+(?:，评价为\s+([^。]+))?", attempt)
                if match and match.group(2):
                    result[match.group(1).strip()] = match.group(2).strip()
        return result

    @staticmethod
    def _plain_verdict(value: str) -> str:
        return {
            "VALIDATED_RESEARCH": "研究验证通过：保留该方向进入下一轮严格验证。",
            "REVISE": "需要修订：有可用线索，但稳定性或证据一致性不足。",
            "REJECT": "拒绝：当前证据不支持保留该候选。",
            "REJECTED": "拒绝：当前证据不支持保留该候选。",
            "SUPPORTED": "研究结果支持该命题。",
            "CONDITIONALLY_SUPPORTED": "仅在明确条件下得到支持。",
        }.get(value, value)

    @staticmethod
    def _raw_experiment_id(value: str) -> str:
        return re.sub(r"（(?:机器复核研究结果|正式 Evidence)）$", "", value).strip()

    @staticmethod
    def _customer_label_text(value: str, labels: Mapping[str, str]) -> str:
        result = value
        for experiment_id, label in sorted(labels.items(), key=lambda item: -len(item[0])):
            result = result.replace(experiment_id, label)
        result = re.sub(r"task_[A-Za-z0-9_]+", "内部实验", result)
        return result

    def _client_findings(self, metrics: Sequence[BoundMetric]) -> list[str]:
        records = self._aggregate_regression(metrics)
        ordered = sorted(
            (item for item in records.items() if item[1]["significant"]),
            key=lambda item: (
                -len(item[1]["significant"]),
                min(value[1] for value in item[1]["significant"]),
                item[0],
            ),
        )[:8]
        rows: list[str] = []
        for term, values in ordered:
            significant = values["significant"]
            directions = {1 if item[0] > 0 else -1 for item in significant}
            direction = "正向" if sum(item[0] for item in significant) > 0 else "负向"
            cross_group = f"{len(significant)} 条路线显著，整体{direction}"
            if len(directions) > 1:
                cross_group = f"{len(significant)} 条路线显著，但方向冲突"
            interpretation = self._plain_term_interpretation(term, direction)
            if len(directions) > 1:
                judgment = "方向冲突，暂不采用"
            elif len(significant) >= 2:
                judgment = "优先进入下一轮验证"
            else:
                judgment = "单路线线索，需要复核"
            rows.append(
                f"| {self._escape(self._pretty_term(term))} | {cross_group} | "
                f"{min(item[1] for item in significant):.3g} | {interpretation} | {judgment} |"
            )
        return rows

    def _plain_term_interpretation(self, term: str, direction: str) -> str:
        return {
            "turnover_rate": "成交更活跃的股票在本样本中随后表现更强，但还没有扣除调仓成本。",
            "roe": "股东资本盈利能力较强的公司显示更好的后续收益关系。",
            "roa": "资产回报率呈负向，与 ROE 线索并不完全一致，可能涉及杠杆或指标口径。",
            "shibor_1m": "利率水平越高，后续收益关系越弱；这仍不是高低利率状态的完整比较。",
            "book_to_market": "估值水平与后续收益有关，但需要确认是否只出现在某个模型中。",
            "pb": "市净率关系在本样本中出现线索，但跨模型稳定性不足。",
        }.get(term, f"该变量在本次样本中呈{direction}关系，需要样本外复核。")

    def _usage_guidance(self, report: CustomerResearchReport) -> list[str]:
        lines = [
            "- **可以使用的部分**：将机器结果支持、且在不同研究路线中方向一致的发现用于下一轮研究决策。",
            "- **暂时不能使用的部分**：统计系数、相关性或事件效应不能直接解释为预期收益，也不能据此生成持仓。",
            "- **当前验证进度**：以报告开头的机器结论为准；未列入机器指标的模型解释不构成证据。",
        ]
        if report.limitations:
            labels = self._experiment_labels(report)
            lines.append("- **最重要的风险**：" + "；".join(
                self._customer_label_text(item, labels) for item in report.limitations[:3]
            ))
        else:
            lines.append("- **最重要的风险**：仍需关注样本外稳定性、交易成本、容量和已知风险暴露。")
        return lines

    def _specific_next_steps(self, report: CustomerResearchReport) -> tuple[str, ...]:
        steps = []
        if report.metrics:
            steps.extend([
                "围绕本轮最一致的机器发现执行独立重放，并保留原假设、样本和成本口径。",
                "根据当前问题对应的输出合同补充样本外、成本、稳定性或反证检验。",
            ])
        else:
            steps.append("先解决覆盖表列出的首个数据或能力缺口，再按原问题重跑，不更换研究目标。")
        question = report.question.casefold()
        if any(token in question for token in ("低相关", "增量", "现有", "正交")):
            steps.append("直接计算候选与 50 日动量的相关系数和残差增量，不再使用零变化代理。")
        if any(token in question for token in ("shibor", "状态", "高低", "环境")):
            steps.append("按决策时可知的 Shibor 划分高低状态，分别报告样本数、收益、IC 和成本。")
        steps.append("在完整冻结历史和实际历史股票池上重放，检查结论是否只由少量股票或单一时期造成。")
        return tuple(steps)

    def _plain_data_summary(self, report: CustomerResearchReport) -> tuple[str, ...]:
        decision = next(
            (item.split("：", 1)[1] for item in report.data_description if item.startswith("决策时间：")),
            "未记录",
        )
        sample_counts: list[int] = []
        for metric in report.metrics:
            if any(
                token in metric.name.casefold()
                for token in ("minimum sample count", "minimum_sample_count")
            ):
                try:
                    sample_counts.append(int(float(metric.display_value)))
                except ValueError:
                    pass
        sample = min(sample_counts) if sample_counts else None
        values = [
            f"研究基准时点：{decision}。",
            f"共有 {len(report.research_process) or len(report.specialists)} 个专业研究组完成独立路线。",
        ]
        if sample is not None:
            values.append(f"回归实验的最低有效样本数为 {sample}。")
        values.append(
            "数据身份、研究源码、审查结果和复现凭证保存在独立机器收据中；客户报告不展示内部哈希。"
        )
        return tuple(values)

    def _headline_conclusion(self, report: CustomerResearchReport) -> str:
        """Lead with the strongest machine-bound finding, not a generic status line."""
        score_groups = []
        labels = self._experiment_labels(report)
        verdicts = self._experiment_verdicts(report)
        for experiment, metrics in self._group_metrics(report.metrics):
            values = self._metric_values(metrics)
            if any(key in values for key in ("ic_mean", "rank_ic", "net_return", "icir")):
                score_groups.append((self._raw_experiment_id(experiment), values))
        validated = [item for item in score_groups if verdicts.get(item[0]) == "VALIDATED_RESEARCH"]
        if validated:
            experiment_id, values = validated[0]
            label = labels.get(experiment_id, "一条候选路线")
            return (
                f"本轮由{label}提出的候选通过可见机器验证："
                + self._joined_metrics(values, (
                    ("ic_mean", "IC 均值"), ("rank_ic", "RankIC"),
                    ("net_return", "成本后收益"), ("icir", "ICIR"),
                ))
                + "。但其他研究路线与分时期/分状态结果并不完全一致，"
                "所以总体结论是保留该方向并修订验证，而不是直接采用。"
            )
        significant: dict[str, list[tuple[float, float]]] = {}
        grouped: dict[tuple[str, str], dict[str, BoundMetric]] = {}
        for item in report.metrics:
            experiment, name = self._split_metric(item.name)
            for prefix in ("coefficient.", "p_value_normal."):
                if name.startswith(prefix):
                    grouped.setdefault((experiment, name[len(prefix):]), {})[prefix[:-1]] = item
                    break
        for (_, term), values in grouped.items():
            coefficient = values.get("coefficient")
            p_value = values.get("p_value_normal")
            if coefficient is None or p_value is None:
                continue
            try:
                coefficient_number = float(coefficient.display_value)
                p_number = float(p_value.display_value)
            except ValueError:
                continue
            if coefficient_number != 0 and p_number <= 0.05:
                significant.setdefault(term, []).append((coefficient_number, p_number))
        if significant:
            ordered = sorted(
                significant,
                key=lambda term: (-len(significant[term]), min(item[1] for item in significant[term]), term),
            )[:3]
            labels = []
            for term in ordered:
                values = significant[term]
                direction = "正向" if sum(item[0] for item in values) > 0 else "负向"
                labels.append(f"{self._pretty_term(term)}呈{direction}关系")
            return (
                "本轮找到值得继续验证的研究方向："
                + "、".join(labels)
                + "。但成本后收益、与既有因子的直接增量性和分状态稳定性尚未完整验证；"
                "当前结论是“进入下一轮验证”，不是“可以直接交易”。"
            )
        return _CONCLUSION_ZH[report.conclusion]

    @staticmethod
    def _evidence_map(metrics: Sequence[BoundMetric]) -> dict[str, str]:
        result: dict[str, str] = {}
        for metric in metrics:
            if metric.evidence_hash not in result:
                result[metric.evidence_hash] = f"E{len(result) + 1}"
        return result

    def _regression_findings(
        self, metrics: Sequence[BoundMetric], evidence_map: Mapping[str, str]
    ) -> list[str]:
        grouped: dict[tuple[str, str], dict[str, BoundMetric]] = {}
        for item in metrics:
            experiment, name = self._split_metric(item.name)
            for prefix in ("coefficient.", "p_value_normal.", "t_statistic."):
                if name.startswith(prefix):
                    term = name[len(prefix):]
                    if term not in {"intercept", "const"}:
                        grouped.setdefault((experiment, term), {})[prefix[:-1]] = item
                    break
        rows: list[str] = []
        for (experiment, term), values in sorted(grouped.items()):
            coefficient = values.get("coefficient")
            p_value = values.get("p_value_normal")
            if coefficient is None or p_value is None:
                continue
            try:
                coefficient_number = float(coefficient.display_value)
                p_number = float(p_value.display_value)
            except ValueError:
                continue
            direction = "正向" if coefficient_number > 0 else "负向" if coefficient_number < 0 else "无方向"
            if coefficient_number == 0 and p_number >= 0.99:
                interpretation = "变量可能缺少有效变化，本轮无法判断。"
            elif p_number <= 0.05:
                interpretation = "本次样本中存在较强关联线索，仍需样本外、成本和稳定性验证。"
            else:
                interpretation = "本次样本未显示稳定关系，不能作为主要支持证据。"
            rows.append(
                f"| {self._escape(experiment)} | {self._escape(self._pretty_term(term))} | {direction} | "
                f"{self._escape(coefficient.display_value)} | {self._escape(p_value.display_value)} | "
                f"{interpretation} | {evidence_map[coefficient.evidence_hash]} |"
            )
        return rows

    def _coverage_rows(self, report: CustomerResearchReport) -> list[str]:
        question = report.question.casefold()
        names = tuple(item.name.casefold() for item in report.metrics)
        has_regression = any("coefficient." in name for name in names)
        labels = self._experiment_labels(report)
        verdicts = self._experiment_verdicts(report)
        score_groups: list[tuple[str, dict[str, str]]] = []
        for experiment, metrics in self._group_metrics(report.metrics):
            values = self._metric_values(metrics)
            if any(key in values for key in ("ic_mean", "rank_ic", "net_return", "icir")):
                score_groups.append((self._raw_experiment_id(experiment), values))

        rows: list[str] = []
        if score_groups:
            directions = []
            evidence = []
            for experiment_id, values in score_groups:
                ic = self._number(values.get("ic_mean"))
                rank = self._number(values.get("rank_ic"))
                pearson = self._number(values.get("pearson_ic"))
                nonzero = [item for item in (ic, rank, pearson) if item is not None and item != 0]
                direction = "正向" if nonzero and all(item > 0 for item in nonzero) else "负向" if nonzero and all(item < 0 for item in nonzero) else "方向冲突"
                directions.append(direction)
                label = labels.get(experiment_id, "一条候选路线")
                prediction_metrics = self._joined_metrics(values, (
                    ("ic_mean", "IC均值"), ("rank_ic", "RankIC"),
                    ("net_return", "成本后收益"),
                ))
                evidence.append(
                    f"{label}：{prediction_metrics}，"
                    f"机器判断为{self._plain_verdict(verdicts.get(experiment_id, '未单独记录'))}"
                )
            if set(directions) == {"正向"}:
                answer = "候选整体呈正向预测关系。"
                judgment = "已回答，但仍需样本外验证"
            elif "正向" in directions:
                answer = "至少一条候选呈正向，但不同路线或 IC 口径存在冲突。"
                judgment = "部分回答，不能合并成单一肯定结论"
            elif set(directions) == {"负向"}:
                answer = "候选方向整体与原假设相反。"
                judgment = "已回答：当前不支持"
            else:
                answer = "现有结果方向不一致，尚不能确认预测关系。"
                judgment = "需要修订"
            rows.append(
                f"| 未来收益预测关系 | {self._escape(answer)} | {self._escape('；'.join(evidence))} | {judgment} |"
            )
        elif has_regression:
            rows.append("| 未来收益预测关系 | 回归中观察到变量关系，但尚未形成可交易评分。 | 已有系数与 p 值，缺少完整 IC、成本和排序评价。 | 初步回答 |")
        else:
            rows.append("| 未来收益预测关系 | 本轮没有可评价的标准结果表。 | 没有对应机器指标。 | 尚未回答 |")

        if any(token in question for token in ("低相关", "增量", "正交", "现有")):
            direct_groups = [
                (experiment_id, values) for experiment_id, values in score_groups
                if "incremental_ic_mean" in values or "max_registered_abs_correlation" in values
            ]
            proxy = any("orthogonal_factor" in name for name in names)
            if direct_groups:
                evidence = "；".join(
                    f"{labels.get(experiment_id, '候选')}："
                    + self._joined_metrics(values, (("incremental_ic_mean", "增量IC均值"), ("max_registered_abs_correlation", "最高绝对相关性")))
                    for experiment_id, values in direct_groups
                )
                zero_correlation = any(self._number(values.get("max_registered_abs_correlation")) == 0 for _, values in direct_groups)
                caveat = "；相关性为0的记录需复核基准因子是否实际进入比较" if zero_correlation else ""
                rows.append(f"| 与既有因子的低相关或增量性 | 候选保留了正的增量 IC 记录，但低相关性结论仍需核实。 | {self._escape(evidence + caveat)} | 部分回答 |")
            elif proxy:
                rows.append("| 与既有因子的低相关或增量性 | 只做了正交代理回归，未直接回答与目标因子的相关性。 | 没有目标因子相关系数或残差增量表。 | 尚未完整回答 |")
            else:
                rows.append("| 与既有因子的低相关或增量性 | 本轮未完成直接比较。 | 没有相关性或残差增量结果。 | 尚未回答 |")

        if any(token in question for token in ("shibor", "状态", "高低", "环境")):
            regime_metrics = {
                name: item.display_value
                for item in report.metrics
                for _, name in (self._split_metric(item.name),)
                if any(token in name.casefold() for token in ("regime", "shibor", "sign_stability", "effect.mean return"))
            }
            direct = bool(regime_metrics)
            proxy = any("shibor" in name for name in names)
            stability_zero = any(
                "sign_stability" in name.casefold() and self._number(value) == 0
                for name, value in regime_metrics.items()
            )
            if direct:
                answer = "高低 Shibor 状态下的效果没有保持稳定。" if stability_zero else "已经形成分状态结果，但仍需结合最弱状态和样本量判断。"
                key_values = "；".join(f"{self._pretty_metric(name)} {value}" for name, value in list(regime_metrics.items())[:6])
                rows.append(f"| 市场状态稳定性 | {answer} | {self._escape(key_values)} | {'已回答：不稳定' if stability_zero else '已回答但需谨慎'} |")
            elif proxy:
                rows.append("| 市场状态稳定性 | Shibor 只作为回归变量或交互项，不能回答高低状态是否稳定。 | 缺少独立分状态收益、IC、成本和样本数。 | 尚未完整回答 |")
            else:
                rows.append("| 市场状态稳定性 | 本轮未进行状态比较。 | 没有状态比较结果。 | 尚未回答 |")

        if any(token in question for token in ("成本", "换手", "流动性", "容量")):
            direct_groups = [
                (experiment_id, values) for experiment_id, values in score_groups
                if "net_return" in values or "turnover" in values
            ]
            proxy = any("coefficient.turnover_rate" in name for name in names)
            if direct_groups:
                evidence = "；".join(
                    f"{labels.get(experiment_id, '候选')}："
                    + self._joined_metrics(values, (("gross_return", "成本前收益"), ("net_return", "成本后收益"), ("turnover", "换手率")))
                    for experiment_id, values in direct_groups
                )
                all_positive = all((self._number(values.get("net_return")) or 0) > 0 for _, values in direct_groups)
                answer = "本轮候选在当前成本模型下均保留正收益，但换手不低，仍需容量和更高成本压力测试。" if all_positive else "至少一条候选扣除成本后失效，不能把成本前结果当成可交易结果。"
                rows.append(f"| 成本、换手与可交易性 | {answer} | {self._escape(evidence)} | {'已回答：当前成本情景通过' if all_positive else '已回答：存在成本失败'} |")
            elif proxy:
                rows.append("| 成本、换手与可交易性 | 换手率只是解释变量，尚未计算候选自身的交易成本。 | 缺少成本后收益和调仓换手。 | 尚未完整回答 |")
            else:
                rows.append("| 成本、换手与可交易性 | 本轮未完成交易性评价。 | 没有成本模型对应结果。 | 尚未回答 |")
        return rows

    def _group_metrics(
        self, metrics: Sequence[BoundMetric]
    ) -> list[tuple[str, list[tuple[str, BoundMetric]]]]:
        grouped: dict[str, list[tuple[str, BoundMetric]]] = {}
        for item in metrics:
            experiment, name = self._split_metric(item.name)
            grouped.setdefault(experiment, []).append((name, item))
        return [(name, grouped[name]) for name in sorted(grouped)]

    @staticmethod
    def _split_metric(name: str) -> tuple[str, str]:
        parts = [part.strip() for part in name.split("·")]
        auxiliary = bool(parts and parts[0] == "辅助研究")
        if auxiliary:
            parts = parts[1:]
        if len(parts) >= 2:
            suffix = "（机器复核研究结果）" if auxiliary else "（正式 Evidence）"
            return parts[0] + suffix, " · ".join(parts[1:])
        suffix = "机器复核研究结果" if auxiliary else "正式 Evidence"
        return f"本次研究（{suffix}）", parts[0]

    @staticmethod
    def _pretty_term(term: str) -> str:
        return {
            "book_to_market": "账面市值比",
            "earnings_yield": "盈利收益率",
            "market_cap": "市值",
            "roe": "净资产收益率（ROE）",
            "roa": "总资产收益率（ROA）",
            "pb": "市净率（PB）",
            "pe_ttm": "滚动市盈率（PE-TTM）",
            "gross_margin": "毛利率",
            "net_margin": "净利率",
            "net_moneyflow_amount": "资金净流入",
            "volume": "成交量",
            "float_market_cap": "流通市值",
            "index_pct_change": "市场指数涨跌幅",
            "turnover_rate": "换手率",
            "momentum_20d": "20 日动量代理",
            "orthogonal_factor": "正交候选因子",
            "orthogonal_factor_x_shibor": "候选因子 × Shibor 交互项",
            "shibor_1m": "1 个月 Shibor",
        }.get(term, term.replace("_", " "))

    def _pretty_metric(self, name: str) -> str:
        prefixes = {
            "coefficient.": "回归系数",
            "p_value_normal.": "p 值",
            "standard_error.": "标准误",
            "t_statistic.": "t 统计量",
            "sample_count.": "有效样本数",
        }
        for prefix, label in prefixes.items():
            if name.startswith(prefix):
                return f"{self._pretty_term(name[len(prefix):])} · {label}（{name}）"
        return name.replace("_", " ")

    @staticmethod
    def _escape(value: str) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    @staticmethod
    def _append_compact_list(lines: list[str], title: str, values: Sequence[str]) -> None:
        lines.extend(["", f"**{title}**", ""])
        lines.extend(f"- {item}" for item in dict.fromkeys(values))

    @staticmethod
    def _append_list(lines: list[str], title: str, values: Sequence[str], *, empty: str = "无。") -> None:
        lines.extend(["", f"## {title}", ""])
        lines.extend((f"- {item}" for item in values) if values else (empty,))


def render_persisted_report(source: str | Path, destination: str | Path) -> str:
    """Atomically rebuild a customer Markdown view from a verified report contract."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    report = CustomerResearchReport.from_dict(payload)
    markdown = ChineseResearchReportComposer().render(report)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=destination_path.name + ".", suffix=".tmp", dir=destination_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(markdown)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return report.artifact_hash


__all__ = [
    "BoundMetric", "ChineseResearchReportComposer", "CustomerResearchReport", "ResearchConclusion",
    "ResearchProcessNarrative", "render_persisted_report",
]
