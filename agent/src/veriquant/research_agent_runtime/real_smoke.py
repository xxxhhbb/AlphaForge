"""One-specialist real-provider smoke for the safe research AgentLoop."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..llm_desks.safety import canonical_hash
from ..research_toolbox import ContentAddressedHandleStore
from .checkpoint import ResearchCancellationToken
from .contracts import ResearchAgentStatus, ResearchAgentTask, ResearchDraftKind
from .factory import SafeVibeAgentLoopFactory
from .real_llm import ConfiguredResearchLLMFactory
from .runtime import VibeAgentLoopAdapter
from .toolbox import InMemoryResearchToolbox, ResearchToolResult


def execute_real_agent_smoke(
    *,
    question: str,
    artifact_root: Path,
    dotenv_path: Path = Path("agent/.env"),
    llm_factory: ConfiguredResearchLLMFactory | None = None,
) -> Mapping[str, Any]:
    """Call the configured model once; persist only redacted metadata and source hash."""

    artifact_root = artifact_root.resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    run_id = "real_agent_smoke_" + canonical_hash({"question": question})[7:19]
    data_summary = {"fields": ["close", "symbol", "decision_time"], "time_semantics": "AS_OF"}
    task = ResearchAgentTask(
        task_id="single_specialist_smoke",
        run_id=run_id,
        specialist_id="market_structure",
        question=question,
        campaign_hash=canonical_hash({"question": question, "mode": "REAL_AGENT_SMOKE"}),
        data_snapshot_hash=canonical_hash(data_summary),
        decision_time=now,
        allowed_tools=("describe_visible_data",),
        max_iterations=3,
        timeout_seconds=300.0,
    )

    toolbox = InMemoryResearchToolbox(
        task=task,
        handlers={
            "describe_visible_data": lambda arguments: ResearchToolResult(
                payload={**data_summary, "query": arguments["query"]},
                available_at=now,
            )
        },
    )
    # Program-controlled minimum investigation.  Its receipt is visible to the
    # outcome even when this strict JSON provider does not issue function calls.
    toolbox.call("describe_visible_data", {"query": "approved visible fields and time semantics"})

    configured = llm_factory or ConfiguredResearchLLMFactory(dotenv_path=dotenv_path)
    store = ContentAddressedHandleStore(artifact_root / "draft_handles")
    runtime = VibeAgentLoopAdapter(
        loop_factory=SafeVibeAgentLoopFactory(
            llm_factory=configured,
            run_root=artifact_root / "vibe_runs",
        ),
        draft_store=store,
    )
    outcome = runtime.run(
        task=task,
        toolbox=toolbox,
        upstream_context={},
        cancellation=ResearchCancellationToken(),
    )

    code_handles = tuple(
        item.content_hash for item in outcome.draft_artifacts if item.kind is ResearchDraftKind.CODE
    )
    source_has_entrypoint = False
    if code_handles:
        source = store.read_bytes(code_handles[0]).decode("utf-8")
        source_has_entrypoint = "def run_research(" in source
    result = {
        "schema_version": "alphaforge.real-research-agent-smoke/v1",
        "run_id": run_id,
        "status": outcome.status.value,
        "authority": outcome.authority,
        "provider": dict(configured.public_view),
        "provider_calls": outcome.provider_calls,
        "tool_receipt_count": len(outcome.investigation_receipts),
        "code_draft_handles": code_handles,
        "source_has_run_research": source_has_entrypoint,
        "source_executed": False,
        "official_evidence_hash": outcome.official_evidence_hash,
        "promotion_hash": outcome.promotion_hash,
        "typed_failure": outcome.failure_code.value if outcome.failure_code is not None else None,
        "failure_origin": outcome.failure_origin,
        "smoke_passed": outcome.status in {ResearchAgentStatus.COMPLETED, ResearchAgentStatus.PARTIAL}
        and bool(code_handles)
        and source_has_entrypoint,
    }
    _atomic_write_json(artifact_root / "result.json", result)
    return result


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent)
    temporary = Path(handle.name)
    try:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AlphaForge real research-agent smoke")
    parser.add_argument("--question", default="研究过去20个交易日价格变化的横截面信号")
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--dotenv", type=Path, default=Path("agent/.env"))
    args = parser.parse_args(argv)
    try:
        result = execute_real_agent_smoke(
            question=args.question,
            artifact_root=args.artifact_root,
            dotenv_path=args.dotenv,
        )
    except Exception:
        # No provider exception text is emitted because it may contain transport
        # details.  Construction failures are represented by this typed shell.
        result = {
            "schema_version": "alphaforge.real-research-agent-smoke/v1",
            "status": "FAILED",
            "typed_failure": "RUNTIME_FAILED",
            "source_executed": False,
            "smoke_passed": False,
        }
        _atomic_write_json(args.artifact_root.resolve() / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("smoke_passed") is True else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["execute_real_agent_smoke", "main"]
