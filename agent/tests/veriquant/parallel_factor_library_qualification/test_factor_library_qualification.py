from __future__ import annotations

import json
from pathlib import Path

import pytest

from veriquant.factor_library_qualification import (
    EvidenceAuthorityClass,
    FactorDagCompatibility,
    FactorExecutionTrust,
    FactorLibraryCatalogBuilder,
    FactorQualificationError,
    FactorSourceKind,
    QualificationStatus,
    query_factor_library,
)


def _definition(
    factor_id: str = "fund_roe",
    *,
    field: str = "fund:roe",
    formula: str = r"\mathrm{zscore}_{x}(\mathrm{ROE})",
    body: str = "def compute(panel):\n    return panel['fund:roe']\n",
    extra_meta: str = "",
) -> str:
    return f'''__alpha_meta__ = {{
    "id": "{factor_id}",
    "theme": ["quality"],
    "formula_latex": r"{formula}",
    "columns_required": ["{field}"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 20,
    "min_warmup_bars": 1,
    {extra_meta}
}}
{body}'''


def _write(root: Path, zoo: str, filename: str, source: str) -> Path:
    target = root / zoo / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return target


def test_real_catalog_is_stable_and_emits_21_static_draft_seeds() -> None:
    builder = FactorLibraryCatalogBuilder()
    first = builder.build(generated_at="2026-07-19T01:00:00+08:00")
    second = builder.build(generated_at="2026-07-20T01:00:00+08:00")

    assert len(first.entries) == 461
    assert first.catalog_hash == second.catalog_hash
    direct = query_factor_library(first, compatibility=FactorDagCompatibility.DIRECT_FACTOR_DAG)
    assert len(direct) == 21
    assert {"price", "volume", "fundamental"} <= {
        "price" if "close" in record.fields else "volume" if "volume" in record.fields else "fundamental"
        for record in direct
    }
    manifest_a = builder.seed_manifest(first, generated_at="one")
    manifest_b = builder.seed_manifest(second, generated_at="two")
    assert len(manifest_a.seeds) == 21
    assert manifest_a.manifest_hash == manifest_b.manifest_hash
    assert all(seed.stable_payload()["authority"] == "DRAFT_ONLY" for seed in manifest_a.seeds)
    rendered = json.dumps(first.to_dict(), sort_keys=True).lower()
    assert "sealed" not in rendered
    assert "official_metrics" not in rendered
    assert "promotion_decision" not in rendered


def test_duplicate_id_and_malformed_definition_fail_closed(tmp_path: Path) -> None:
    duplicate_root = tmp_path / "duplicate"
    _write(duplicate_root, "one", "a.py", _definition())
    _write(duplicate_root, "two", "b.py", _definition())
    with pytest.raises(FactorQualificationError) as duplicate:
        FactorLibraryCatalogBuilder(duplicate_root).build()
    assert duplicate.value.code == "DUPLICATE_FACTOR_ID"

    malformed_root = tmp_path / "malformed"
    _write(malformed_root, "one", "a.py", "__alpha_meta__ = {")
    with pytest.raises(FactorQualificationError) as malformed:
        FactorLibraryCatalogBuilder(malformed_root).build()
    assert malformed.value.code == "MALFORMED_SOURCE"


def test_path_traversal_and_source_hash_change_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "zoo"
    source = _write(root, "safe", "roe.py", _definition())
    outside = _write(tmp_path / "outside", "safe", "roe.py", _definition())
    builder = FactorLibraryCatalogBuilder(root)
    with pytest.raises(FactorQualificationError) as traversal:
        builder.build(paths=(outside,))
    assert traversal.value.code == "PATH_TRAVERSAL"

    catalog = builder.build(paths=(source,))
    frozen_hash = catalog.entries[0].source_hash
    source.write_text(_definition(body="def compute(panel):\n    return 0\n"), encoding="utf-8")
    with pytest.raises(FactorQualificationError) as changed:
        builder.verify_catalog(catalog)
    assert changed.value.code == "SOURCE_HASH_MISMATCH"
    with pytest.raises(FactorQualificationError) as mismatch:
        builder.build(paths=(source,), expected_source_hashes={"fund_roe": frozen_hash})
    assert mismatch.value.code == "SOURCE_HASH_MISMATCH"


@pytest.mark.parametrize(
    ("filename", "source", "expected"),
    (
        (
            "callback.py",
            _definition(body="callback = lambda value: value\ndef compute(panel):\n    return callback(panel)\n"),
            FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY,
        ),
        (
            "conditional.py",
            _definition(body="def compute(panel):\n    if panel:\n        return panel['fund:roe']\n    return None\n"),
            FactorDagCompatibility.NEEDS_DETERMINISTIC_OPERATOR,
        ),
        (
            "unknown.py",
            _definition(
                factor_id="alpha101_001",
                field="close",
                formula="ts_argmax(close, 5)",
                body="def compute(panel):\n    return ts_argmax(panel['close'], 5)\n",
            ),
            FactorDagCompatibility.NEEDS_DETERMINISTIC_OPERATOR,
        ),
        (
            "factor.py",
            _definition(),
            FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY,
        ),
    ),
)
def test_python_callback_conditional_unknown_operator_and_factor_py_are_not_direct(
    tmp_path: Path, filename: str, source: str, expected: FactorDagCompatibility
) -> None:
    root = tmp_path / filename.replace(".py", "")
    path = _write(root, "zoo", filename, source)
    record = FactorLibraryCatalogBuilder(root).build(paths=(path,)).entries[0]
    assert record.compatibility is expected
    assert record.compatibility is not FactorDagCompatibility.DIRECT_FACTOR_DAG
    assert record.execution_trust is FactorExecutionTrust.NEVER_TRUSTED_EXECUTION


def test_legacy_bench_is_auxiliary_and_never_official() -> None:
    digest = "sha256:" + "0" * 64
    record = FactorLibraryCatalogBuilder.auxiliary_result_record(
        factor_id="legacy.bench.001",
        source_kind=FactorSourceKind.ALPHA_BENCH_RESULT,
        source_hash=digest,
        metadata_hash=digest,
    )
    assert record.qualification_status is QualificationStatus.AUXILIARY_ONLY
    assert record.compatibility is FactorDagCompatibility.UNSAFE_AUXILIARY_ONLY
    assert record.evidence_authority is EvidenceAuthorityClass.AUXILIARY_ONLY
    assert record.execution_trust is FactorExecutionTrust.NEVER_TRUSTED_EXECUTION
    rendered = json.dumps(record.to_dict(), sort_keys=True)
    assert '"evidence_authority": "OFFICIAL"' not in rendered
    assert "official_metrics" not in rendered.lower()


def test_forbidden_sealed_or_authority_metadata_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "zoo"
    path = _write(root, "safe", "roe.py", _definition(extra_meta='"sealed_metrics": {"ic": 1.0},'))
    with pytest.raises(FactorQualificationError) as forbidden:
        FactorLibraryCatalogBuilder(root).build(paths=(path,))
    assert forbidden.value.code == "FORBIDDEN_CATALOG_FIELD"
