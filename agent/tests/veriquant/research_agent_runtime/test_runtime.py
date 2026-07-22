from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from veriquant.research_agent_runtime import (
    BranchCheckpointStore,
    InMemoryResearchToolbox,
    ResearchAgentFailureCode,
    ResearchAgentOutcome,
    ResearchAgentStatus,
    ResearchAgentTask,
    ResearchCancellationToken,
    ResearchSwarmSchedulerAdapter,
    ResearchToolResult,
    VeriQuantResearchContextBuilder,
    VibeAgentLoopAdapter,
)
from veriquant.research_run_controller import ResearchRunLifecycle, ResearchRunManifest
from veriquant.research_toolbox.handles import ContentAddressedHandleStore


NOW = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def _task(
    index: int,
    *,
    run_id: str = "run_runtime",
    depends_on: tuple[str, ...] = (),
    input_from: dict[str, str] | None = None,
) -> ResearchAgentTask:
    return ResearchAgentTask(
        task_id=f"task_{index}",
        run_id=run_id,
        specialist_id=f"specialist_{index}",
        question="研究一个可证伪的横截面信号",
        campaign_hash=_hash("a"),
        data_snapshot_hash=_hash("b"),
        decision_time=NOW,
        allowed_tools=(f"research_view_{index}",),
        depends_on=depends_on,
        input_from=input_from or {},
    )


def _manifest(tasks: tuple[ResearchAgentTask, ...]) -> ResearchRunManifest:
    first = tasks[0]
    return ResearchRunManifest(
        run_id=first.run_id,
        campaign_hash=first.campaign_hash,
        intent_hash=_hash("c"),
        data_plan_hash=_hash("d"),
        route_plan_hash=_hash("e"),
        policy_hash=_hash("f"),
        code_hash=_hash("0"),
        selected_group_ids=tuple(sorted(task.specialist_id for task in tasks)),
        candidate_request_hashes=(),
        created_at=NOW,
        runtime_version="research_agent_runtime_v1",
        provider_id="fake_provider",
        model_id="fake_model",
        data_snapshot_hash=first.data_snapshot_hash,
        current_stage=ResearchRunLifecycle.RESEARCHING,
    )


def _toolbox(task: ResearchAgentTask) -> InMemoryResearchToolbox:
    def handler(arguments):
        return ResearchToolResult(
            payload={"summary": "frozen visible research rows", "query": arguments["query"]},
            available_at=NOW,
        )

    return InMemoryResearchToolbox(task=task, handlers={task.allowed_tools[0]: handler})


class _FakeLoop:
    def __init__(self, task, toolbox, calls, *, forbidden=False, delay=0.0):
        self.task = task
        self.toolbox = toolbox
        self.calls = calls
        self.forbidden = forbidden
        self.delay = delay
        self.cancelled = False

    def run(self, user_message, history=None, session_id=""):
        self.calls.append((self.task.task_id, user_message, session_id))
        if self.delay:
            time.sleep(self.delay)
        self.toolbox.call(self.task.allowed_tools[0], {"query": "describe frozen coverage"})
        payload = {
            "plan": "先调查数据，再提出并自审候选。",
            "code_drafts": ["score = frame['close'].pct_change(20)"],
            "status": "COMPLETED",
            "authority": "DRAFT_ONLY",
        }
        if self.forbidden:
            payload["official_metrics"] = {"icir": 99}
        return {"status": "success", "content": json.dumps(payload), "iterations": 2}

    def cancel(self):
        self.cancelled = True


class _Factory:
    def __init__(self, *, forbidden=False, delays=None):
        self.calls = []
        self.forbidden = forbidden
        self.delays = delays or {}

    def __call__(self, *, task, toolbox, event_callback):
        return _FakeLoop(
            task,
            toolbox,
            self.calls,
            forbidden=self.forbidden,
            delay=self.delays.get(task.task_id, 0.0),
        )


class _StubRuntime:
    def __init__(self, status: ResearchAgentStatus):
        self.status = status
        self.calls = 0

    def run(self, *, task, toolbox, upstream_context, cancellation):
        self.calls += 1
        failure = self.status in {
            ResearchAgentStatus.FAILED,
            ResearchAgentStatus.BLOCKED,
            ResearchAgentStatus.CANCELLED,
        }
        return ResearchAgentOutcome(
            task_id=task.task_id,
            task_hash=task.artifact_hash,
            specialist_id=task.specialist_id,
            status=self.status,
            plan_hash=None if failure else _hash("9"),
            investigation_receipts=(),
            draft_artifacts=(),
            provider_calls=1,
            tool_calls=0,
            failure_code=ResearchAgentFailureCode.RUNTIME_FAILED if failure else None,
            failure_origin="STUB_FAILURE" if failure else None,
        )


def _run_scheduler(tmp_path: Path, tasks, runtimes, *, cancellation=None, resume=False):
    tasks = tuple(tasks)
    scheduler = ResearchSwarmSchedulerAdapter(
        checkpoint_store=BranchCheckpointStore(tmp_path),
        max_workers=6,
    )
    return scheduler.run(
        manifest=_manifest(tasks),
        tasks=tasks,
        runtimes=runtimes,
        toolboxes={task.task_id: _toolbox(task) for task in tasks},
        cancellation=cancellation,
        resume=resume,
    )


def test_context_and_toolbox_are_exactly_scoped():
    task = _task(1)
    toolbox = _toolbox(task)
    prompt = VeriQuantResearchContextBuilder().build(
        task,
        tool_definitions=toolbox.definitions,
        upstream_context={},
    )
    assert '"authority":"DRAFT_ONLY"' in prompt
    assert task.allowed_tools[0] in prompt
    assert "official evidence" in prompt
    with pytest.raises(Exception):
        InMemoryResearchToolbox(task=task, handlers={"another_tool": lambda _: None})
    with pytest.raises(Exception):
        toolbox.call("another_tool", {"query": "coverage"})


def test_vibe_adapter_produces_only_plan_investigation_and_code_drafts(tmp_path):
    task = _task(1)
    factory = _Factory()
    store = ContentAddressedHandleStore(tmp_path / "drafts")
    outcome = VibeAgentLoopAdapter(loop_factory=factory, draft_store=store).run(
        task=task,
        toolbox=_toolbox(task),
        upstream_context={},
        cancellation=ResearchCancellationToken(),
    )
    assert outcome.status is ResearchAgentStatus.COMPLETED
    assert outcome.plan_hash is not None
    assert outcome.tool_calls == 1
    assert {item.kind.value for item in outcome.draft_artifacts} == {"PLAN", "CODE"}
    assert outcome.official_evidence_hash is None
    assert outcome.promotion_hash is None
    code = next(item for item in outcome.draft_artifacts if item.kind.value == "CODE")
    assert store.read_bytes(code.content_hash) == b"score = frame['close'].pct_change(20)"
    plan = next(item for item in outcome.draft_artifacts if item.kind.value == "PLAN")
    assert json.loads(store.read_bytes(plan.content_hash))["plan"] == "先调查数据，再提出并自审候选。"


def test_vibe_adapter_records_typed_data_gaps_and_transparent_proxies(tmp_path):
    task = _task(1)

    class GapLoop(_FakeLoop):
        def run(self, user_message, history=None, session_id=""):
            raw = super().run(user_message, history, session_id)
            payload = json.loads(raw["content"])
            payload["data_gaps"] = [{
                "concept": "analyst_expectation_revision",
                "reason_code": "SOURCE_UNAVAILABLE",
                "requested_fields": ["estimate_revision"],
                "attempted_tools": [task.allowed_tools[0]],
                "detail": "批准数据源未返回该字段。",
            }]
            payload["proxy_specs"] = [{
                "requested_concept": "analyst_expectation_revision",
                "proxy_name": "announcement_surprise_proxy",
                "source_fields": ["close", "volume"],
                "rationale": "使用公告后价量反应作为透明代理。",
                "limitations": ["不能替代分析师一致预期"],
            }]
            raw["content"] = json.dumps(payload)
            return raw

    class GapFactory:
        def __call__(self, *, task, toolbox, event_callback):
            return GapLoop(task, toolbox, [])

    outcome = VibeAgentLoopAdapter(
        loop_factory=GapFactory(),
        draft_store=ContentAddressedHandleStore(tmp_path / "drafts"),
    ).run(
        task=task,
        toolbox=_toolbox(task),
        upstream_context={},
        cancellation=ResearchCancellationToken(),
    )
    assert outcome.status is ResearchAgentStatus.COMPLETED
    assert outcome.data_gaps[0].reason_code == "SOURCE_UNAVAILABLE"
    assert outcome.proxy_specs[0].source_fields == ("close", "volume")
    assert outcome.proxy_specs[0].limitations == ("不能替代分析师一致预期",)


def test_code_source_without_content_store_fails_closed():
    task = _task(1)
    outcome = VibeAgentLoopAdapter(loop_factory=_Factory()).run(
        task=task,
        toolbox=_toolbox(task),
        upstream_context={},
        cancellation=ResearchCancellationToken(),
    )
    assert outcome.status is ResearchAgentStatus.FAILED
    assert outcome.failure_code is ResearchAgentFailureCode.ARTIFACT_STORE_UNAVAILABLE


def test_vibe_adapter_rejects_model_claimed_official_metrics():
    task = _task(1)
    outcome = VibeAgentLoopAdapter(loop_factory=_Factory(forbidden=True)).run(
        task=task,
        toolbox=_toolbox(task),
        upstream_context={},
        cancellation=ResearchCancellationToken(),
    )
    assert outcome.status is ResearchAgentStatus.FAILED
    assert outcome.failure_code is ResearchAgentFailureCode.INVALID_MODEL_OUTPUT


@pytest.mark.parametrize("count", range(1, 7))
def test_one_to_six_specialists_run_with_stable_order(tmp_path, count):
    tasks = tuple(_task(index) for index in range(1, count + 1))
    factories = {task.specialist_id: _Factory() for task in tasks}
    result = _run_scheduler(
        tmp_path,
        tasks,
        {
            key: VibeAgentLoopAdapter(
                loop_factory=value,
                draft_store=ContentAddressedHandleStore(tmp_path / "drafts" / key),
            )
            for key, value in factories.items()
        },
    )
    assert result.status is ResearchAgentStatus.COMPLETED
    assert tuple(item.task_id for item in result.outcomes) == tuple(sorted(task.task_id for task in tasks))
    assert sum(len(factory.calls) for factory in factories.values()) == count


def test_failed_branch_does_not_stop_independent_branch(tmp_path):
    tasks = (_task(1), _task(2))
    failed = _StubRuntime(ResearchAgentStatus.FAILED)
    completed = _StubRuntime(ResearchAgentStatus.COMPLETED)
    result = _run_scheduler(
        tmp_path,
        tasks,
        {tasks[0].specialist_id: failed, tasks[1].specialist_id: completed},
    )
    assert [item.status for item in result.outcomes] == [
        ResearchAgentStatus.FAILED,
        ResearchAgentStatus.COMPLETED,
    ]
    assert failed.calls == completed.calls == 1
    assert result.status is ResearchAgentStatus.PARTIAL


def test_failed_dependency_blocks_downstream_without_calling_it(tmp_path):
    upstream = _task(1)
    downstream = _task(2, depends_on=(upstream.task_id,), input_from={"parent": upstream.task_id})
    failed = _StubRuntime(ResearchAgentStatus.FAILED)
    never_called = _StubRuntime(ResearchAgentStatus.COMPLETED)
    result = _run_scheduler(
        tmp_path,
        (upstream, downstream),
        {upstream.specialist_id: failed, downstream.specialist_id: never_called},
    )
    by_id = {item.task_id: item for item in result.outcomes}
    assert by_id[downstream.task_id].status is ResearchAgentStatus.BLOCKED
    assert by_id[downstream.task_id].failure_code is ResearchAgentFailureCode.DEPENDENCY_BLOCKED
    assert never_called.calls == 0


def test_resume_does_not_duplicate_a_committed_branch(tmp_path):
    task = _task(1)
    factory = _Factory()
    runtime = VibeAgentLoopAdapter(loop_factory=factory)
    first = _run_scheduler(tmp_path, (task,), {task.specialist_id: runtime})
    second = _run_scheduler(tmp_path, (task,), {task.specialist_id: runtime}, resume=True)
    assert len(factory.calls) == 1
    assert first.artifact_hash == second.artifact_hash


def test_explicit_resume_can_continue_a_cancelled_branch(tmp_path):
    task = _task(1)
    runtime = _StubRuntime(ResearchAgentStatus.COMPLETED)
    cancelled = ResearchCancellationToken()
    cancelled.cancel()
    first = _run_scheduler(
        tmp_path,
        (task,),
        {task.specialist_id: runtime},
        cancellation=cancelled,
    )
    assert first.outcomes[0].status is ResearchAgentStatus.CANCELLED
    assert runtime.calls == 0
    resumed = _run_scheduler(tmp_path, (task,), {task.specialist_id: runtime}, resume=True)
    assert resumed.outcomes[0].status is ResearchAgentStatus.COMPLETED
    assert runtime.calls == 1


def test_parallel_completion_order_does_not_change_result_hash(tmp_path):
    tasks = (_task(1), _task(2), _task(3))
    first_factories = {
        task.specialist_id: _Factory(delays={task.task_id: 0.01 * (4 - index)})
        for index, task in enumerate(tasks, start=1)
    }
    second_factories = {
        task.specialist_id: _Factory(delays={task.task_id: 0.01 * index})
        for index, task in enumerate(tasks, start=1)
    }
    first = _run_scheduler(
        tmp_path / "first",
        tasks,
        {key: VibeAgentLoopAdapter(loop_factory=value) for key, value in first_factories.items()},
    )
    second = _run_scheduler(
        tmp_path / "second",
        tasks,
        {key: VibeAgentLoopAdapter(loop_factory=value) for key, value in second_factories.items()},
    )
    assert first.artifact_hash == second.artifact_hash


def test_checkpoint_tamper_fails_closed(tmp_path):
    task = _task(1)
    runtime = _StubRuntime(ResearchAgentStatus.COMPLETED)
    _run_scheduler(tmp_path, (task,), {task.specialist_id: runtime})
    checkpoint = tmp_path / task.run_id / "branches" / task.task_id / "outcome.json"
    checkpoint.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception):
        _run_scheduler(tmp_path, (task,), {task.specialist_id: runtime}, resume=True)
