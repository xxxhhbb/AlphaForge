from __future__ import annotations

import json
import re
from pathlib import Path

from veriquant.integration.c8_real_v1 import C8_PROVIDER_CLASS, C8_RUN_ID, C8_SOURCE_CLASS, C8RealV1Runner


REPO_ROOT = Path(__file__).parents[4]


def test_c8_real_approved_proxy_loop_produces_challenger_and_20d_replay(tmp_path: Path) -> None:
    result = C8RealV1Runner(repo_root=REPO_ROOT).run(
        output_root=tmp_path / "c8",
        seed=7,
        publish_workbench=False,
    )

    assert result["status"] == "PASSED"
    assert result["data_source_class"] == C8_PROVIDER_CLASS
    assert result["source_class"] == C8_SOURCE_CLASS
    assert result["official_tushare_source"] is False
    assert result["network_used"] is False
    assert result["snapshot"]["row_count"] == 116
    assert result["official_evidence"]["official_evidence_run"] is True
    assert 1 <= result["official_evidence"]["factor_count"] <= 3
    assert result["official_evidence"]["sealed_aggregate_used"] is True
    assert result["official_evidence"]["sealed_raw_exposed"] is False
    promotion_states = {
        factor["factor_id"]: factor["promotion_state"]
        for factor in result["official_evidence"]["factors"]
    }
    assert "REJECTED" in promotion_states.values()
    assert "CHALLENGER" in promotion_states.values()
    assert result["promotion"]["challenger_created"] is True
    assert result["promotion"]["receipt"]["source_authority"] == "INDEPENDENT_PROMOTION_SERVICE"
    assert result["promotion"]["receipt"]["state"] == "CHALLENGER"
    assert result["shadow_replay"]["sessions_committed"] == 20
    assert result["shadow_replay"]["daily_vintage_count"] == 20
    assert result["shadow_replay"]["forced_interruption"] is True
    assert result["shadow_replay"]["records_after_interrupt"] == 9
    assert result["shadow_replay"]["resume_completed"] is True
    assert result["shadow_replay"]["rerun_no_duplicate_journal"] is True
    assert result["shadow_replay"]["final_nav"] > 0
    assert result["security"]["status"] == "PASSED"
    assert all(result["invariants"].values())

    written = tmp_path / "c8" / C8_RUN_ID / "result.json"
    assert written.is_file()
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["artifact_hash"] == result["artifact_hash"]


def test_c8_real_loop_is_idempotent_for_same_frozen_inputs(tmp_path: Path) -> None:
    runner = C8RealV1Runner(repo_root=REPO_ROOT)
    first = runner.run(output_root=tmp_path / "c8", seed=7, publish_workbench=False)
    second = runner.run(output_root=tmp_path / "c8", seed=7, publish_workbench=False)

    assert second["artifact_hash"] == first["artifact_hash"]
    assert second["shadow_replay"]["rerun_no_duplicate_journal"] is True
    assert second["shadow_replay"]["recovery_stage_start_count"] == 2


def test_c8_result_never_masquerades_as_official_tushare(tmp_path: Path) -> None:
    result = C8RealV1Runner(repo_root=REPO_ROOT).run(
        output_root=tmp_path / "c8",
        seed=7,
        publish_workbench=False,
    )
    rendered = json.dumps(result, sort_keys=True, ensure_ascii=False)

    assert "OFFICIAL_TUSHARE" not in rendered
    assert "APPROVED_PROXY_DATA_VENDOR" in rendered
    assert "TUSHARE_COMPATIBLE_PROXY" in rendered
    assert "SEALED_RAW_VALUE" not in rendered
    assert re.search(r"(sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,})", rendered) is None
    assert "/live/" not in rendered.lower()
