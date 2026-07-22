from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.api import alphaforge_campaign_routes as routes
from src.api.alphaforge_campaign_routes import (
    confirm_campaign,
    create_campaign_draft,
    get_campaign_llm_provider_status,
    get_campaign_run,
    list_factor_library,
    reset_alphaforge_campaign_state,
    route_paths,
    summarize_routes,
)
from src.veriquant.alphaforge_llm import AlphaForgeLLMConfigLoader
from src.veriquant.contracts import ContractValidationError, FailureCode
from src.veriquant.llm_desks.api import DeterministicProposalProvider
from src.veriquant.real_llm_provider.client import ProviderProbeResult, ProviderStatus


def run_async(coro):
    return asyncio.run(coro)


def test_campaign_routes_create_confirm_and_return_selected_desk_run() -> None:
    reset_alphaforge_campaign_state()

    draft = run_async(
        create_campaign_draft(
            {
                "question": "最近动量因子为什么失效？",
                "depth": "STANDARD_RESEARCH",
                "interactionStyle": "STANDARD",
            }
        )
    )
    assert draft["state"] == "NEEDS_CONFIRMATION"
    assert draft["mode"] == "DIAGNOSE_FACTOR_PORTFOLIO"
    assert draft["allowsPromotion"] is False

    confirmed = run_async(
        confirm_campaign(
            {
                "draftId": draft["draftId"],
                "answers": {"portfolio_or_existing_factor_ids": "factor_ids:momentum.v1"},
            }
        )
    )
    assert confirmed["state"] == "CONFIRMED"
    campaign_id = confirmed["campaignId"]

    run = run_async(get_campaign_run(campaign_id))
    assert run["campaignId"] == campaign_id
    assert run["state"] == "ROUTED"
    assert run["checkpointState"] == "PENDING"
    assert run["trialsUsed"] == 0
    assert 1 <= len(run["selectedDesks"]) <= 3
    assert all(desk["status"] == "WAITING" for desk in run["selectedDesks"])
    assert all(stage["status"] == "WAITING" for desk in run["selectedDesks"] for stage in desk["stages"])
    rendered = json.dumps(run, ensure_ascii=False).lower()
    for forbidden in ("sealed_metric", "official_metrics", "target_weights", "orders", "broker", "ledger"):
        assert forbidden not in rendered


def test_campaign_start_runs_offline_acceptance_idempotently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_alphaforge_campaign_state()
    monkeypatch.setattr(routes, "_campaign_run_output_root", lambda campaign_id: tmp_path / "runs" / campaign_id)
    monkeypatch.setattr(routes, "_workbench_artifact_root", lambda: tmp_path / "workbench")

    draft = run_async(
        create_campaign_draft(
            {
                "question": "帮我找新的20日股票因子",
                "depth": "STANDARD_RESEARCH",
                "interactionStyle": "STANDARD",
            }
        )
    )
    confirmed = run_async(confirm_campaign({"draftId": draft["draftId"]}))
    campaign_id = confirmed["campaignId"]

    started = run_async(routes.start_campaign_run(campaign_id))
    repeated = run_async(routes.start_campaign_run(campaign_id))

    assert started == repeated
    assert started["campaignId"] == campaign_id
    assert started["state"] == "COMPLETED"
    assert started["checkpointState"] == "REACHED"
    assert 1 <= len(started["selectedDesks"]) <= 3
    assert started["artifactHash"].startswith("sha256:")
    assert started["factorDagHash"].startswith("sha256:")
    assert started["workbenchDetailHash"].startswith("sha256:")
    assert started["shadowStatus"] == "READY"
    assert Path(started["reportPath"]).is_file()
    assert Path(started["reportPath"]).name == "campaign_report.md"
    campaign_report = json.loads((tmp_path / "runs" / campaign_id / "campaign_report.json").read_text(encoding="utf-8"))
    assert campaign_report["campaign_id"] == campaign_id
    assert campaign_report["artifact_hash"] == started["artifactHash"]
    assert campaign_report["runtime_manifest_hash"].startswith("sha256:")
    assert campaign_report["offline_acceptance_hash"].startswith("sha256:")
    assert (tmp_path / "runs" / campaign_id / "campaign_runtime.json").is_file()
    assert run_async(get_campaign_run(campaign_id)) == started

    rendered = json.dumps(started, ensure_ascii=False).lower()
    for forbidden in ("sealed_metric", "official_metrics", "target_weights", "orders", "broker", "raw_sealed"):
        assert forbidden not in rendered


def test_quick_exploration_start_runs_draft_runtime_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_alphaforge_campaign_state()
    monkeypatch.setattr(routes, "_campaign_run_output_root", lambda campaign_id: tmp_path / "runs" / campaign_id)
    monkeypatch.setattr(routes, "_workbench_artifact_root", lambda: tmp_path / "workbench")

    draft = run_async(
        create_campaign_draft(
            {
                "question": "快速帮我找几个新的20日股票因子草案",
                "depth": "QUICK_EXPLORATION",
                "interactionStyle": "STANDARD",
            }
        )
    )
    confirmed = run_async(confirm_campaign({"draftId": draft["draftId"]}))
    campaign_id = confirmed["campaignId"]

    started = run_async(routes.start_campaign_run(campaign_id))

    assert started["state"] == "COMPLETED"
    assert started["artifactHash"].startswith("sha256:")
    assert started["reportPath"] == f"/veriquant/workbench?run_id={campaign_id}"
    assert started["workbenchPath"] == f"/veriquant/workbench?run_id={campaign_id}"
    assert started["workbenchDetailHash"].startswith("sha256:")
    assert (tmp_path / "runs" / campaign_id / "campaign_runtime.json").is_file()
    assert not (tmp_path / "runs" / campaign_id / "result.json").exists()
    assert (tmp_path / "workbench" / "index.json").is_file()
    rendered = json.dumps(started, ensure_ascii=False).lower()
    assert "quick_exploration" in rendered
    assert "official_metrics" not in rendered


def test_llm_provider_status_is_redacted_and_credential_free(monkeypatch: pytest.MonkeyPatch) -> None:
    class Loader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def load(self):
            return AlphaForgeLLMConfigLoader(
                environ={
                    "ENABLE_LLM_RESEARCH": "1",
                    "OPENAI_BASE_URL": "https://llm.example/v1",
                    "OPENAI_API_KEY": "credential-must-not-render",
                    "LANGCHAIN_MODEL_NAME": "alpha-draft-model",
                }
            ).load()

    monkeypatch.setattr(routes, "AlphaForgeLLMConfigLoader", Loader)

    payload = run_async(get_campaign_llm_provider_status())

    assert payload == {"status": "AVAILABLE", "host": "llm.example", "model": "alpha-draft-model"}
    assert "credential-must-not-render" not in json.dumps(payload, ensure_ascii=False)


def test_real_llm_start_uses_explicit_runtime_mode_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_alphaforge_campaign_state()
    monkeypatch.setattr(routes, "_campaign_run_output_root", lambda campaign_id: tmp_path / "runs" / campaign_id)
    monkeypatch.setattr(routes, "_workbench_artifact_root", lambda: tmp_path / "workbench")

    class Loader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def load(self):
            return AlphaForgeLLMConfigLoader(
                environ={
                    "ENABLE_LLM_RESEARCH": "1",
                    "OPENAI_BASE_URL": "https://llm.example/v1",
                    "OPENAI_API_KEY": "credential-must-not-render",
                    "LANGCHAIN_MODEL_NAME": "alpha-draft-model",
                }
            ).load()

    monkeypatch.setattr(routes, "AlphaForgeLLMConfigLoader", Loader)
    monkeypatch.setattr(
        routes,
        "probe_provider",
        lambda configuration: ProviderProbeResult(
            status=ProviderStatus.AVAILABLE,
            endpoint_host="llm.example",
            model="alpha-draft-model",
        ),
    )
    monkeypatch.setattr(routes, "build_openai_compatible_provider", lambda configuration: DeterministicProposalProvider())

    draft = run_async(
        create_campaign_draft(
            {
                "question": "快速帮我找几个新的20日股票因子草案",
                "depth": "QUICK_EXPLORATION",
                "interactionStyle": "STANDARD",
            }
        )
    )
    confirmed = run_async(confirm_campaign({"draftId": draft["draftId"]}))
    campaign_id = confirmed["campaignId"]

    started = run_async(routes.start_campaign_run(campaign_id, {"runtimeMode": "REAL_LLM"}))

    assert started["state"] == "COMPLETED"
    assert started["runtimeMode"] == "REAL_LLM"
    assert all(desk["runtimeMode"] == "REAL_LLM" for desk in started["selectedDesks"])
    assert (tmp_path / "runs" / campaign_id / "campaign_runtime.real_llm.json").is_file()
    reset_alphaforge_campaign_state()
    reloaded = run_async(get_campaign_run(campaign_id))
    assert reloaded["runtimeMode"] == "REAL_LLM"
    assert reloaded["selectedDesks"] == started["selectedDesks"]
    assert reloaded["reportPath"] == f"/veriquant/workbench?run_id={campaign_id}"
    assert reloaded["workbenchDetailHash"].startswith("sha256:")
    rendered = json.dumps(started, ensure_ascii=False).lower()
    for forbidden in ("credential-must-not-render", "api_key", "authorization", "sealed_metric", "broker", "ledger"):
        assert forbidden not in rendered


def test_real_llm_standard_run_reloads_latest_runtime_instead_of_fixture_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_alphaforge_campaign_state()
    monkeypatch.setattr(routes, "_campaign_run_output_root", lambda campaign_id: tmp_path / "runs" / campaign_id)
    monkeypatch.setattr(routes, "_workbench_artifact_root", lambda: tmp_path / "workbench")

    class Loader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def load(self):
            return AlphaForgeLLMConfigLoader(
                environ={
                    "ENABLE_LLM_RESEARCH": "1",
                    "OPENAI_BASE_URL": "https://llm.example/v1",
                    "OPENAI_API_KEY": "credential-must-not-render",
                    "LANGCHAIN_MODEL_NAME": "alpha-draft-model",
                }
            ).load()

    monkeypatch.setattr(routes, "AlphaForgeLLMConfigLoader", Loader)
    monkeypatch.setattr(
        routes,
        "probe_provider",
        lambda configuration: ProviderProbeResult(
            status=ProviderStatus.AVAILABLE,
            endpoint_host="llm.example",
            model="alpha-draft-model",
        ),
    )
    monkeypatch.setattr(routes, "build_openai_compatible_provider", lambda configuration: DeterministicProposalProvider())

    draft = run_async(
        create_campaign_draft(
            {
                "question": "帮我找一个20日A股动量类Alpha因子",
                "depth": "STANDARD_RESEARCH",
                "interactionStyle": "STANDARD",
            }
        )
    )
    confirmed = run_async(confirm_campaign({"draftId": draft["draftId"]}))
    campaign_id = confirmed["campaignId"]

    started = run_async(routes.start_campaign_run(campaign_id, {"runtimeMode": "REAL_LLM"}))
    reloaded = run_async(get_campaign_run(campaign_id))

    assert reloaded == started
    assert started["runtimeMode"] == "REAL_LLM"
    assert 1 <= len(started["selectedDesks"]) <= 3
    assert all(desk["runtimeMode"] == "REAL_LLM" for desk in started["selectedDesks"])
    assert len(started["researchOutputs"]) == len(started["selectedDesks"])
    assert all(output["authority"] == "DRAFT_ONLY" for output in started["researchOutputs"])
    assert all(output["hypothesis"] for output in started["researchOutputs"])
    assert all(output["factor_draft"] for output in started["researchOutputs"])
    assert all(output["compile_smoke"]["is_production_pit"] is False for output in started["researchOutputs"])
    assert all(output["visible_evaluation_smoke"]["is_official_evidence"] is False for output in started["researchOutputs"])
    assert started["reportPath"] == f"/veriquant/workbench?run_id={campaign_id}"
    assert not (tmp_path / "runs" / campaign_id / "result.json").exists()
    rendered = json.dumps(started, ensure_ascii=False)
    assert "offline acceptance fixture intentionally keeps at least one branch incomplete" not in rendered
    assert "alternative_data" not in {desk["id"] for desk in started["selectedDesks"]}


def test_real_llm_blocked_runtime_does_not_fake_offline_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset_alphaforge_campaign_state()
    monkeypatch.setattr(routes, "_campaign_run_output_root", lambda campaign_id: tmp_path / "runs" / campaign_id)
    monkeypatch.setattr(routes, "_workbench_artifact_root", lambda: tmp_path / "workbench")

    class Loader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def load(self):
            return AlphaForgeLLMConfigLoader(
                environ={
                    "ENABLE_LLM_RESEARCH": "1",
                    "OPENAI_BASE_URL": "https://llm.example/v1",
                    "OPENAI_API_KEY": "credential-must-not-render",
                    "LANGCHAIN_MODEL_NAME": "alpha-draft-model",
                }
            ).load()

    class BlockingProvider(DeterministicProposalProvider):
        def propose_hypothesis(self, **kwargs):
            raise ContractValidationError(FailureCode.PROVIDER_SCHEMA_REJECTED, "synthetic schema rejection")

    monkeypatch.setattr(routes, "AlphaForgeLLMConfigLoader", Loader)
    monkeypatch.setattr(
        routes,
        "probe_provider",
        lambda configuration: ProviderProbeResult(
            status=ProviderStatus.AVAILABLE,
            endpoint_host="llm.example",
            model="alpha-draft-model",
        ),
    )
    monkeypatch.setattr(routes, "build_openai_compatible_provider", lambda configuration: BlockingProvider())

    draft = run_async(
        create_campaign_draft(
            {
                "question": "帮我找一个20日A股动量类Alpha因子",
                "depth": "STANDARD_RESEARCH",
                "interactionStyle": "STANDARD",
            }
        )
    )
    confirmed = run_async(confirm_campaign({"draftId": draft["draftId"]}))
    campaign_id = confirmed["campaignId"]

    started = run_async(routes.start_campaign_run(campaign_id, {"runtimeMode": "REAL_LLM"}))
    reloaded = run_async(get_campaign_run(campaign_id))

    assert started == reloaded
    assert started["state"] == "BLOCKED"
    assert started["runtimeMode"] == "REAL_LLM"
    assert started["reportPath"] == f"/veriquant/workbench?run_id={campaign_id}"
    assert started["workbenchDetailHash"].startswith("sha256:")
    assert (tmp_path / "runs" / campaign_id / "campaign_runtime.real_llm.json").is_file()
    assert not (tmp_path / "runs" / campaign_id / "result.json").exists()
    index = json.loads((tmp_path / "workbench" / "index.json").read_text(encoding="utf-8"))
    assert any(row["run_id"] == campaign_id and row["status"] == "BLOCKED" for row in index["runs"])
    assert {desk["status"] for desk in started["selectedDesks"]} == {"BLOCKED"}
    rendered = json.dumps(started, ensure_ascii=False)
    assert "offline acceptance fixture intentionally keeps at least one branch incomplete" not in rendered
    assert "不会用 deterministic offline acceptance fixture 冒充" in rendered


def test_unknown_draft_confirmation_fails_closed() -> None:
    reset_alphaforge_campaign_state()

    with pytest.raises(Exception) as exc:
        run_async(confirm_campaign({"draftId": "draft_missing"}))

    assert getattr(exc.value, "status_code", None) == 404


def test_factor_library_route_is_static_read_only_metadata() -> None:
    payload = run_async(list_factor_library())

    assert payload["total"] == 461
    assert len(payload["items"]) == 461
    assert payload["catalogHash"].startswith("sha256:")
    rendered = json.dumps(payload["items"][:10], ensure_ascii=False).lower()
    for forbidden in ("source_code", "official_metrics", "promotion", "target_weights", "orders", "sealed"):
        assert forbidden not in rendered


def test_route_manifest_has_no_live_or_broker_authority() -> None:
    paths = route_paths()
    assert "/veriquant/campaign/drafts" in paths
    assert "/veriquant/campaign/runs/{campaign_id}/start" in paths
    assert "/veriquant/factor-library" in paths
    rendered = json.dumps(summarize_routes(paths), sort_keys=True).lower()

    assert "deferred_until_standalone_success" in rendered
    for forbidden in ("/live", "broker", "order", "oauth", "mandate"):
        assert forbidden not in rendered
