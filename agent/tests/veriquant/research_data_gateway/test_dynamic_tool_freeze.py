from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from src.agent.tools import BaseTool, ToolRegistry

from veriquant.data import MarketSnapshot, SnapshotRow
from veriquant.integration.research_os_v2 import FrozenValidationDataRepository
from veriquant.research_agent_runtime import ResearchAgentTask
from veriquant.research_data_gateway.dynamic_tool_freeze import (
    DynamicVibeToolFreezeHarness,
    load_authority_manifest,
)
from veriquant.research_toolbox.handles import ContentAddressedHandleStore
from veriquant.research_toolbox.vibe import VibeResearchToolbox
from veriquant.research_validation_pipeline_v2.models import FrozenValidationData


UTC = timezone.utc


def h(char: str) -> str:
    return "sha256:" + char * 64


class _PayloadTool(BaseTool):
    repeatable = True
    is_readonly = True
    parameters = {
        "type": "object",
        "properties": {"code": {"type": "string"}},
        "required": ["code"],
        "additionalProperties": False,
    }

    def __init__(self, *, name: str, payload: dict) -> None:
        self.name = name
        self.description = "read frozen fixture"
        self.payload = payload

    def execute(self, **kwargs):
        assert kwargs["code"] == "000001.SZ"
        return json.dumps(self.payload, ensure_ascii=False)


def _snapshot() -> MarketSnapshot:
    start = datetime(2026, 1, 1, 15, tzinfo=UTC)
    sessions = tuple(start + timedelta(days=day) for day in range(75))
    rows = tuple(
        SnapshotRow(
            event_time=session,
            symbol=symbol,
            values={"close": 10.0 + day, "volume": 1000.0 + day},
        )
        for day, session in enumerate(sessions)
        for symbol in ("000001.SZ", "000002.SZ")
    )
    return MarketSnapshot(
        dataset_id="dynamic_fixture",
        decision_time=sessions[-1] + timedelta(hours=2),
        start_time=sessions[0],
        fields=("close", "volume"),
        rows=rows,
        historical_universe={
            session.isoformat(): ("000001.SZ", "000002.SZ") for session in sessions
        },
        coverage_by_field={"close": 1.0, "volume": 1.0},
    )


def _toolbox(tmp_path, snapshot):
    registry = ToolRegistry()
    registry.register(_PayloadTool(
        name="get_stock_news",
        payload={
            "ok": True,
            "source": "eastmoney",
            "data": {
                "code": "000001.SZ",
                "articles": [{
                    "published": "2026-02-10 10:30:00",
                    "title": "正常新闻标题",
                    "snippet": "只读研究摘要",
                    "source": "fixture",
                    "url": "https://example.invalid/not-persisted",
                }],
            },
        },
    ))
    registry.register(_PayloadTool(
        name="get_research_reports",
        payload={
            "ok": True,
            "source": "eastmoney",
            "data": {
                "code": "000001.SZ",
                "reports": [{
                    "publish_date": "2026-02-20",
                    "title": "研究报告",
                    "rating": "增持",
                    "brokerage": "fixture",
                }],
            },
        },
    ))
    task = ResearchAgentTask(
        task_id="task_dynamic_freeze",
        run_id="run_dynamic_freeze",
        specialist_id="alternative_data",
        question="研究新闻与研报信号",
        campaign_hash=h("a"),
        data_snapshot_hash=snapshot.artifact_hash,
        decision_time=snapshot.decision_time,
        allowed_tools=("get_research_reports", "get_stock_news"),
        max_iterations=3,
    )
    store = ContentAddressedHandleStore(tmp_path / "handles")
    toolbox = VibeResearchToolbox(
        task=task,
        handle_store=store,
        registry=registry,
        allow_network=True,
        clock=lambda: snapshot.decision_time,
    )
    toolbox.call("get_stock_news", {"code": "000001.SZ"})
    toolbox.call("get_research_reports", {"code": "000001.SZ"})
    return toolbox, store


def test_dynamic_vibe_receipts_materialize_pit_auxiliary_features_and_replay(tmp_path):
    snapshot = _snapshot()
    toolbox, store = _toolbox(tmp_path, snapshot)
    harness = DynamicVibeToolFreezeHarness()

    first = harness.freeze(
        snapshot=snapshot,
        receipts=toolbox.recorded_receipts,
        handle_store=store,
    )
    second = harness.freeze(
        snapshot=snapshot,
        receipts=toolbox.recorded_receipts,
        handle_store=store,
    )
    assert first.artifact_hash == second.artifact_hash
    assert first.authority_ceiling == "AUXILIARY_ONLY"
    assert len(first.documents) == 2

    labels = {
        f"{row.event_time.isoformat()}|{row.symbol}": 0.01
        for row in snapshot.rows
    }
    base = FrozenValidationData(
        snapshot=snapshot,
        labels=labels,
        qualification_hash=h("b"),
        source_label="APPROVED_PROXY_DATA_VENDOR",
    )
    materialized, manifest = harness.materialize(
        base=base,
        bundle=first,
        output_root=tmp_path / "materialized",
    )
    reloaded = FrozenValidationDataRepository(tmp_path / "materialized").load()
    assert reloaded is not None
    assert reloaded.snapshot.artifact_hash == materialized.snapshot.artifact_hash
    assert manifest["authority_ceiling"] == "AUXILIARY_ONLY"
    assert load_authority_manifest(
        tmp_path / "materialized",
        snapshot_hash=materialized.snapshot.artifact_hash,
    )["dynamic_bundle_hash"] == first.artifact_hash

    rows = {
        (row.event_time, row.symbol): row for row in materialized.snapshot.rows
    }
    before_news = datetime(2026, 2, 9, 15, tzinfo=UTC)
    after_news = datetime(2026, 2, 11, 15, tzinfo=UTC)
    # Exact timestamps affect only later rows; unqueried symbols remain
    # missing instead of being silently filled with zero.
    assert rows[(before_news, "000001.SZ")].values["news_count_20_sessions"] == 0
    assert rows[(after_news, "000001.SZ")].values["news_count_20_sessions"] == 1
    assert rows[(after_news, "000002.SZ")].values["news_count_20_sessions"] is None
    # Date-only reports become available conservatively at the following
    # frozen session close, never on their nominal publication date.
    report_date = datetime(2026, 2, 20, 15, tzinfo=UTC)
    next_session = report_date + timedelta(days=1)
    assert rows[(report_date, "000001.SZ")].values["research_report_count_60_sessions"] == 0
    assert rows[(next_session, "000001.SZ")].values["research_report_count_60_sessions"] == 1


def test_dynamic_freeze_fails_closed_when_receipt_cannot_recover_envelope(tmp_path):
    snapshot = _snapshot()
    toolbox, store = _toolbox(tmp_path, snapshot)
    broken = toolbox.recorded_receipts[0]
    from dataclasses import replace

    broken = replace(broken, result_hash=h("f"))
    import pytest

    with pytest.raises(ValueError):
        DynamicVibeToolFreezeHarness().freeze(
            snapshot=snapshot,
            receipts=(broken,),
            handle_store=store,
        )
