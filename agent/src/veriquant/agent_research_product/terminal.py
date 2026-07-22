"""Terminal-first entry for the single Vibe-AgentLoop research product path."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DATA_ROOT = (
    REPO_ROOT / ".autobuild" / "veriquant" / "research500_harness"
    / "real_data_canary_20240601_0731" / "market_spine"
)
DEFAULT_ROOT = REPO_ROOT / ".autobuild" / "veriquant" / "agent_research_product_runs"
DEFAULT_RUNTIME_RECEIPT = (
    REPO_ROOT / ".autobuild" / "veriquant" / "research500_harness"
    / "product_canary" / "command.json"
)
DEFAULT_CATALOG = REPO_ROOT / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json"
SPECIALISTS = (
    "alternative_data", "expectations_events", "fundamental",
    "macro_regime", "market_structure", "meta_alpha",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AlphaForge Vibe-AgentLoop research product")
    parser.add_argument("--question", required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=900.0, help="兼容参数；每组硬超时由任务合同控制")
    parser.add_argument("--depth", default="标准研究")
    parser.add_argument("--interaction-style", default="标准式")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--dotenv", type=Path, default=REPO_ROOT / "agent" / ".env")
    parser.add_argument("--label-horizon", type=int, default=20)
    parser.add_argument("--specialist", action="append", choices=SPECIALISTS)
    parser.add_argument("--allow-dynamic-tools", action="store_true")
    parser.add_argument(
        "--vibe-tool", action="append",
        help="只启用指定的 Vibe 只读研究工具；可重复使用，隐含允许这些工具联网。",
    )
    parser.add_argument("--print-report", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--reuse-existing", action="store_true",
        help="从同一问题已提交的内容寻址草案恢复；仅在真实沙箱失败时允许一次模型修订",
    )
    parser.add_argument("--progress-interval", type=float, default=10.0)
    return parser


def _select_specialists(question: str) -> tuple[str, ...]:
    from .planner import ResearchTaskPlanner

    return ResearchTaskPlanner(catalog_path=DEFAULT_CATALOG).compile(question).selected_specialists


def _output_kind(question: str) -> str:
    from .planner import ResearchTaskPlanner

    return ResearchTaskPlanner(catalog_path=DEFAULT_CATALOG).compile(question).output_kind


def _runtime_identity(path: Path) -> tuple[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    image = str(value.get("docker_image") or "")
    image_id = str(value.get("docker_image_id") or "")
    if not image or not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError("pinned Docker runtime receipt is unavailable")
    return image, image_id


def run(args: argparse.Namespace) -> tuple[int, dict[str, Any], str]:
    from ..research_toolbox import VIBE_READONLY_RESEARCH_TOOLS
    from .canary import AgentResearchProductCanary, ProductCanaryConfiguration
    from .planner import ResearchTaskPlanner

    image, image_id = _runtime_identity(DEFAULT_RUNTIME_RECEIPT)
    task_plan = ResearchTaskPlanner(catalog_path=DEFAULT_CATALOG).compile(args.question)
    requested_specialists = tuple(dict.fromkeys(args.specialist or task_plan.selected_specialists))
    # The task plan keeps lead-first business priority, while the execution
    # contract requires a stable-sorted identity for deterministic hashes.
    # Preserve both meanings instead of treating planner order as identity.
    lead_specialist = requested_specialists[0] if args.specialist else task_plan.lead_specialist
    specialists = tuple(sorted(requested_specialists))
    requested_vibe_tools = tuple(sorted(set(
        args.vibe_tool or (VIBE_READONLY_RESEARCH_TOOLS if args.allow_dynamic_tools else ())
    )))
    if not set(requested_vibe_tools).issubset(VIBE_READONLY_RESEARCH_TOOLS):
        raise ValueError("--vibe-tool 只能选择已批准的只读研究工具")
    identity = json.dumps(
        {
            "question": args.question,
            "seed": args.seed,
            "specialists": specialists,
            "specialist_output_kinds": task_plan.specialist_output_kinds,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    identity_root = Path(args.root).resolve() / hashlib.sha256(identity).hexdigest()[:16]
    resume_sources: tuple[tuple[str, Path, str], ...] = ()
    run_root = identity_root
    if args.reuse_existing:
        prior_path, resume_sources = _recoverable_sources(identity_root, specialists)
        recovery_identity = hashlib.sha256(
            prior_path.read_bytes() + b"\0docker-recovery-v3-bounded-revision-report"
        ).hexdigest()[:16]
        run_root = identity_root / "recovery" / recovery_identity
    configuration = ProductCanaryConfiguration(
        data_root=Path(args.data_root),
        artifact_root=run_root,
        question=args.question,
        docker_image=image,
        docker_image_id=image_id,
        label_horizon_sessions=args.label_horizon,
        seed=args.seed,
        specialist_id=lead_specialist,
        specialist_ids=specialists,
        specialist_output_kinds=task_plan.specialist_output_kinds,
        resume_sources=resume_sources,
        output_kind=task_plan.output_kind,
        dotenv_path=Path(args.dotenv),
        vibe_tool_names=requested_vibe_tools,
        allow_vibe_network_tools=bool(requested_vibe_tools),
    )
    result = _run_with_progress(
        AgentResearchProductCanary(configuration),
        run_root=run_root,
        specialists=specialists,
        interval=float(args.progress_interval),
        recovering=bool(resume_sources),
    )
    report = Path(result.report_path).read_text(encoding="utf-8")
    payload = result.to_dict()
    payload["selected_specialists"] = list(specialists)
    payload["runtime"] = "VIBE_AGENT_LOOP_AGENT_RESEARCH_PRODUCT_V1"
    payload["dynamic_research_tools"] = list(requested_vibe_tools)
    payload["research_task_plan"] = task_plan.to_dict()
    payload["recovered_from_existing_drafts"] = bool(resume_sources)
    return (0 if result.lifecycle in {"COMPLETED", "PARTIAL"} else 2), payload, report


_SPECIALIST_ZH = {
    "alternative_data": "另类数据研究组",
    "expectations_events": "预期与事件研究组",
    "fundamental": "基本面研究组",
    "macro_regime": "宏观状态研究组",
    "market_structure": "市场行为与交易可行性研究组",
    "meta_alpha": "组合增量研究组",
}


def _recoverable_sources(
    identity_root: Path,
    specialists: tuple[str, ...],
) -> tuple[Path, tuple[tuple[str, Path, str], ...]]:
    candidates = sorted(
        identity_root.rglob("product_result.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        raise ValueError("没有可恢复的已提交研究草案")
    prior_path = candidates[0]
    value = json.loads(prior_path.read_text(encoding="utf-8"))
    experiments = value.get("experiments") if isinstance(value, dict) else None
    if not isinstance(experiments, list):
        raise ValueError("已存在运行缺少可恢复实验收据")
    rank = {
        "OFFICIAL_EVIDENCE": 5,
        "AUXILIARY_ONLY": 4,
        "PARTIAL": 3,
        "NEGATIVE": 2,
        "DATA_GAP": 1,
        "BLOCKED": 0,
    }
    selected: dict[str, tuple[int, int, str]] = {}
    for index, item in enumerate(experiments):
        if not isinstance(item, dict) or not item.get("source_hash"):
            continue
        specialist_id = str(item.get("specialist_id"))
        candidate = (rank.get(str(item.get("status")), -1), index, str(item["source_hash"]))
        if specialist_id not in selected or candidate[:2] > selected[specialist_id][:2]:
            selected[specialist_id] = candidate
    recovered = []
    for specialist_id in specialists:
        source_hash = selected.get(specialist_id, (-1, -1, ""))[2]
        if not source_hash.startswith("sha256:"):
            raise ValueError("已存在运行没有覆盖全部研究组的源码草案")
        digest = source_hash[7:]
        matches = sorted(identity_root.glob(f"**/draft_sources/{digest[:2]}/{digest[2:]}"))
        if not matches:
            raise ValueError("已存在运行的内容寻址源码文件缺失")
        source_path = matches[-1]
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != digest:
            raise ValueError("已存在运行的源码 hash 不一致")
        recovered.append((specialist_id, source_path, source_hash))
    return prior_path, tuple(recovered)


def _run_with_progress(canary, *, run_root: Path, specialists: tuple[str, ...], interval: float, recovering: bool):
    if interval <= 0:
        return canary.run()
    stop = threading.Event()
    monitor = threading.Thread(
        target=_progress_loop,
        kwargs={
            "root": run_root,
            "specialists": specialists,
            "interval": min(30.0, max(0.5, interval)),
            "stop": stop,
            "recovering": recovering,
        },
        name="alphaforge-terminal-progress",
        daemon=True,
    )
    monitor.start()
    try:
        return canary.run()
    finally:
        stop.set()
        monitor.join(timeout=2.0)


def _progress_loop(*, root: Path, specialists: tuple[str, ...], interval: float, stop: threading.Event, recovering: bool) -> None:
    action = "恢复已提交草案" if recovering else "启动真实模型研究"
    print(
        f"[1/7] 研究计划已形成：{'、'.join(_SPECIALIST_ZH[item] for item in specialists)}",
        file=sys.stderr,
        flush=True,
    )
    print(f"[2/7] 正在读取并校验冻结数据；随后{action}", file=sys.stderr, flush=True)
    seen: set[str] = set()
    while not stop.is_set():
        _emit_progress(root, seen)
        stop.wait(interval)
    _emit_progress(root, seen)


def _emit_progress(root: Path, seen: set[str]) -> None:
    for path in sorted(root.glob("research_runs/*/product_request.json")):
        _emit_once(seen, "frozen-data-ready", "[2/7] 冻结数据与研究身份已校验，开始专业研究")
    for path in sorted(root.glob("agent_runs/*/specialists/*/llm_usage.json")):
        value = _safe_json(path)
        totals = value.get("totals") if isinstance(value.get("totals"), dict) else {}
        calls = int(totals.get("calls") or 0)
        specialist = path.parent.name
        if calls:
            _emit_once(
                seen,
                f"llm:{specialist}:{calls}",
                f"[3/7] {_SPECIALIST_ZH.get(specialist, specialist)}真实模型已完成 {calls} 次有界迭代",
            )
    for path in sorted(root.glob("*/branches/*/outcome.json")):
        value = _safe_json(path)
        specialist = str((value.get("payload") or {}).get("specialist_id") or path.parent.name.removeprefix("task_"))
        _emit_once(seen, f"outcome:{specialist}", f"[3/7] {_SPECIALIST_ZH.get(specialist, specialist)}已提交研究代码")
    for path in sorted(root.glob("research_runs/*/experiments/*/review.json")):
        value = _safe_json(path)
        status = str(value.get("status", "UNKNOWN"))
        _emit_once(seen, f"review:{path.parent.name}:{status}", f"[4/7] {path.parent.name} 机械审查：{status}")
    for path in sorted(root.glob("research_runs/*/experiments/*/runner.json")):
        value = _safe_json(path)
        status = str(value.get("status", "UNKNOWN"))
        failure = str(value.get("failure_code") or "")
        suffix = f"（{failure}）" if failure else ""
        _emit_once(seen, f"runner:{path.parent.name}:{status}:{failure}", f"[5/7] {path.parent.name} 隔离实验：{status}{suffix}")
    for path in sorted(root.glob("research_runs/*/experiments/*/evidence*.json")):
        _emit_once(seen, f"evidence:{path}", f"[6/7] {path.parent.name} 已生成机器 Evidence")
    for path in sorted(root.glob("research_runs/*/product_result.json")):
        value = _safe_json(path)
        lifecycle = str(value.get("lifecycle", "UNKNOWN"))
        _emit_once(seen, f"result:{path}:{lifecycle}", f"[7/7] 正式报告已生成：{lifecycle}")


def _safe_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _emit_once(seen: set[str], identity: str, message: str) -> None:
    if identity in seen:
        return
    seen.add(identity)
    print(message, file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        code, payload, report = run(args)
    except Exception as exc:
        payload = {
            "status": "FAILED",
            "failure_code": "AGENT_RESEARCH_PRODUCT_FAILED",
            "failure_type": type(exc).__name__,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print("AlphaForge 千因研究院 · Agent Research Product")
        print(f"运行时：{payload['runtime']}")
        print(f"研究组：{'、'.join(payload['selected_specialists'])}")
        print(f"状态：{payload['lifecycle']}")
        print(f"实验：{len(payload['experiments'])}")
        print(f"报告：{payload['report_path']}")
        if args.print_report:
            print("\n" + report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run"]
