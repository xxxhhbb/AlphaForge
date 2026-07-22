"""Deterministic customer report contracts for Agent research runs.

The composer translates already-bound machine facts into Chinese.  It never
calculates a metric and cannot upgrade an admission or promotion state.
"""

from __future__ import annotations

import json
import re
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
    r"sealed(?:_|\s)*(?:metric|data|value)|target_weights?|orders?|broker_credentials?)"
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

        sections.extend(["", "## 最值得关注的研究发现", ""])
        findings = self._client_findings(report.metrics)
        if findings:
            sections.extend([
                "下表只保留最能回答问题的结果。这里的“统计线索”表示样本中观察到了关系，"
                "不表示已经证明可以赚钱。p 值越小，说明该关系越不像随机波动；它仍不是收益率。",
                "",
                "| 发现 | 跨组表现 | 最小 p 值 | 通俗解读 | 当前判断 |",
                "|---|---|---:|---|---|",
                *findings,
            ])
        elif report.metrics:
            sections.extend(["| 指标 | 结果 |", "|---|---:|"])
            sections.extend(
                f"| {self._escape(item.name)} | {self._escape(item.display_value)} |"
                for item in report.metrics[:12]
            )
        else:
            sections.append("本结论没有可冒充的官方指标；具体原因见“没有完成什么”和下一步建议。")

        sections.extend(["", "## 原问题回答到了什么程度", ""])
        sections.extend(["| 需要回答的部分 | 本轮状态 | 说明 |", "|---|---|---|"])
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

        self._append_list(sections, "代码、方法与反证审查", report.review_summary)
        self._append_list(sections, "稳健性与反证", report.robustness_checks)
        self._append_list(
            sections, "反对证据", self._customer_evidence_text(report.opposing_evidence),
            empty="未发现可用文字描述的决定性反对证据。",
        )
        self._append_list(sections, "数据和方法限制", report.limitations)

        if report.metrics:
            sections.extend(["", "## 完整机器指标", ""])
            for index, (_, metrics) in enumerate(self._group_metrics(report.metrics), start=1):
                sections.extend([
                    f"### 实验 {index}", "",
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
        points.append(
            "本轮真正完成的是多变量关系检验；尚未完成完整因子回测、成本后收益、"
            "与 50 日动量的直接相关性以及高低 Shibor 分组比较。"
        )
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
        return [
            "- **可以使用的部分**：把跨研究路线重复出现的变量作为下一轮候选方向。",
            "- **暂时不能使用的部分**：不能把当前回归系数直接解释为预期收益，也不能据此生成持仓。",
            "- **最重要的风险**：换手率本身可能意味着更高交易成本；ROE 与 ROA 的结论不完全一致；"
            "当前动量控制变量缺少有效变化。",
            "- **当前证据等级**：机器已经复核统计结果，但完整交易检验和独立晋级评审尚未完成。",
        ]

    def _specific_next_steps(self, report: CustomerResearchReport) -> tuple[str, ...]:
        steps = [
            "把本轮较一致的变量组合成明确的每日股票评分，并进行滚动样本外评价。",
            "计算 RankIC、ICIR、分位数组合收益、换手、价差和冲击成本，确认统计关系能否转化为成本后收益。",
        ]
        question = report.question.casefold()
        if any(token in question for token in ("低相关", "增量", "现有", "正交")):
            steps.append("直接计算候选与 50 日动量的相关系数和残差增量，不再使用零变化代理。")
        if any(token in question for token in ("shibor", "状态", "高低", "环境")):
            steps.append("按决策时可知的 Shibor 划分高低状态，分别报告样本数、收益、IC 和成本。")
        steps.append("在更长时间和更大历史股票池上重放，检查结论是否只由少量股票或单一时期造成。")
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
        has_score = any(
            token in name for name in names
            for token in ("rankic", "icir", "成本后收益", "net_return")
        )
        rows = [
            "| 未来收益预测关系 | "
            + ("已做完整机器评价" if has_score else "已做初步机器检验" if has_regression else "尚未检验")
            + " | "
            + ("已形成 IC、收益或成本类指标。" if has_score else "当前主要是回归关系，尚未形成完整可交易因子评价。" if has_regression else "没有对应标准结果表。")
            + " |"
        ]
        if any(token in question for token in ("低相关", "增量", "正交", "现有")):
            direct = any(token in name for name in names for token in ("相关性", "correlation", "incremental_ic"))
            proxy = any("orthogonal_factor" in name for name in names)
            rows.append(
                "| 与既有因子的低相关或增量性 | "
                + ("已直接检验" if direct else "已做近似检验" if proxy else "尚未检验")
                + " | "
                + ("存在直接相关性或增量指标。" if direct else "使用正交候选回归，但没有直接报告与目标因子的相关系数。" if proxy else "没有相关性或残差增量结果。")
                + " |"
            )
        if any(token in question for token in ("shibor", "状态", "高低", "环境")):
            direct = any(token in name for name in names for token in ("regime", "状态正向比例", "最弱市场状态"))
            proxy = any("shibor" in name for name in names)
            rows.append(
                "| 市场状态稳定性 | "
                + ("已分状态检验" if direct else "已做交互项检验" if proxy else "尚未检验")
                + " | "
                + ("已形成分状态样本和效应比较。" if direct else "Shibor 已进入回归，但未形成高低状态的独立表现表。" if proxy else "没有状态比较结果。")
                + " |"
            )
        if any(token in question for token in ("成本", "换手", "流动性", "容量")):
            direct = any(token in name for name in names for token in ("成本后收益", "spread_cost", "impact_cost", "换手率"))
            proxy = any("coefficient.turnover_rate" in name for name in names)
            rows.append(
                "| 成本、换手与可交易性 | "
                + ("已直接检验" if direct else "仅有流动性代理" if proxy else "尚未检验")
                + " | "
                + ("已有成本或换手机器指标。" if direct else "换手率只作为解释变量，尚未计算候选交易成本。" if proxy else "没有成本模型对应的结果。")
                + " |"
            )
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


__all__ = [
    "BoundMetric", "ChineseResearchReportComposer", "CustomerResearchReport", "ResearchConclusion",
    "ResearchProcessNarrative",
]
