from __future__ import annotations

import json
from pathlib import Path

import pytest

from veriquant.research500_harness import (
    CoverageInputs,
    Research500Level1Harness,
    load_catalog,
)
from veriquant.research_toolbox.contracts import (
    RegisteredContextReceipt,
    canonical_hash,
)
from veriquant.research_toolbox.registry import (
    AUXILIARY_DATA_IDS,
    OFFICIAL_DATA_IDS,
    REGISTERED_CONTEXT_DATA_IDS,
    SPECIALIST_TOOL_ALLOWLISTS,
)


ROOT = Path(__file__).resolve().parents[4]
CATALOG = ROOT / ".autobuild" / "veriquant" / "factor_question_500" / "catalog.json"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _real_coverage() -> CoverageInputs:
    official = [
        {
            "source_id": data_id,
            "status": "SUCCESS",
            "authority": "OFFICIAL_INPUT",
            "content_hash": canonical_hash({"real": data_id}),
        }
        for data_id in sorted(OFFICIAL_DATA_IDS)
    ]
    auxiliary = tuple(
        {
            "source_id": data_id,
            "status": "SUCCESS",
            "authority": "AUXILIARY_RESEARCH_EVIDENCE",
            "content_hash": canonical_hash({"auxiliary": data_id}),
        }
        for data_id in sorted(AUXILIARY_DATA_IDS)
    )
    registered = tuple(
        RegisteredContextReceipt(data_id=data_id, content_hash=canonical_hash({"registered": data_id}))
        for data_id in sorted(REGISTERED_CONTEXT_DATA_IDS)
    )
    return CoverageInputs(
        capability_report={"status": "READY", "artifact_hash": HASH_A, "provenance": "REAL"},
        gateway_manifest={"status": "READY", "artifact_hash": HASH_B, "receipts": official},
        auxiliary_receipts=auxiliary,
        registered_context_receipts=registered,
    )


def test_catalog_is_exact_unique_25_by_20() -> None:
    catalog = load_catalog(CATALOG)
    assert len(catalog.questions) == 500
    assert len(catalog.families) == 25
    assert len(catalog.dimensions) == 20
    assert len({item.question_id for item in catalog.questions}) == 500
    assert len({(item.family_id, item.test_id) for item in catalog.questions}) == 500


def test_level1_has_static_route_and_authorized_tools_for_all_500(tmp_path: Path) -> None:
    verdict = Research500Level1Harness().run(
        catalog_path=CATALOG,
        output_dir=tmp_path,
        coverage_inputs=_real_coverage(),
    )
    capability = json.loads((tmp_path / "capability_catalog.json").read_text(encoding="utf-8"))
    assert capability["row_count"] == 500
    for row in capability["rows"]:
        assert row["static_status"] == "STATIC_COVERED"
        assert row["output_kind"]
        assert row["experiment_type"]
        assert row["report_type"]
        assigned = set()
        for specialist, tools in row["tool_assignments"].items():
            assert set(tools) <= set(SPECIALIST_TOOL_ALLOWLISTS[specialist])
            assigned.update(tools)
        assert assigned == set(row["required_tools"])
    assert verdict["level1_static_status"] == "PASSED"
    assert verdict["status"] == "NOT_PASSED"


def test_stable_artifact_hashes_and_bytes(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    harness = Research500Level1Harness()
    verdict_a = harness.run(catalog_path=CATALOG, output_dir=first, coverage_inputs=_real_coverage())
    verdict_b = harness.run(catalog_path=CATALOG, output_dir=second, coverage_inputs=_real_coverage())
    names = {
        "capability_catalog.json",
        "data_coverage_matrix.json",
        "question_results.jsonl",
        "family_canaries.json",
        "dimension_canaries.json",
        "final_verdict.json",
    }
    assert verdict_a == verdict_b
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_missing_catalog_item_fails_closed(tmp_path: Path) -> None:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    raw["questions"].pop()
    raw["question_count"] = 499
    raw["catalog_hash"] = canonical_hash(raw["questions"])
    damaged = tmp_path / "catalog.json"
    damaged.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly 500"):
        load_catalog(damaged)


def test_duplicate_question_id_fails_closed(tmp_path: Path) -> None:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    raw["questions"][1]["question_id"] = raw["questions"][0]["question_id"]
    raw["catalog_hash"] = canonical_hash(raw["questions"])
    damaged = tmp_path / "catalog.json"
    damaged.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_catalog(damaged)


def test_synthetic_or_fixture_coverage_cannot_claim_real_or_tested(tmp_path: Path) -> None:
    inputs = _real_coverage()
    synthetic = CoverageInputs(
        capability_report={"status": "READY", "artifact_hash": HASH_A, "provenance": "SYNTHETIC"},
        gateway_manifest={
            **inputs.gateway_manifest,
            "provenance": "OFFLINE_FIXTURE",
        },
        auxiliary_receipts=inputs.auxiliary_receipts,
        registered_context_receipts=inputs.registered_context_receipts,
    )
    verdict = Research500Level1Harness().run(
        catalog_path=CATALOG, output_dir=tmp_path, coverage_inputs=synthetic
    )
    results = (tmp_path / "question_results.jsonl").read_text(encoding="utf-8")
    assert verdict["status"] == "NOT_PASSED"
    assert "COVERED_AND_TESTED" not in results
    assert all(not json.loads(line)["is_real_data_tested"] for line in results.splitlines())


def test_missing_real_canaries_never_passes(tmp_path: Path) -> None:
    verdict = Research500Level1Harness().run(
        catalog_path=CATALOG, output_dir=tmp_path, coverage_inputs=_real_coverage()
    )
    assert verdict["status"] == "NOT_PASSED"
    assert verdict["family_canary_status_counts"] == {"NOT_RUN": 25}
    assert verdict["dimension_canary_status_counts"] == {"NOT_RUN": 20}
    assert "REAL_FAMILY_CANARIES_NOT_RUN" in verdict["failure_reasons"]
    assert "REAL_DIMENSION_CANARIES_NOT_RUN" in verdict["failure_reasons"]


def test_no_coverage_proof_produces_real_four_state_unresolved_matrix(tmp_path: Path) -> None:
    Research500Level1Harness().run(catalog_path=CATALOG, output_dir=tmp_path)
    matrix = json.loads((tmp_path / "data_coverage_matrix.json").read_text(encoding="utf-8"))
    statuses = {row["status"] for row in matrix["rows"]}
    assert statuses == {"UNRESOLVED"}
    assert all(row["coverage_hash"].startswith("sha256:") for row in matrix["rows"])
