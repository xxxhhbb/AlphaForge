from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.veriquant.factor_library import (
    CatalogBuildError,
    CatalogFailureCode,
    FactorLibraryCatalogBuilder,
    FactorQualificationStatus,
    build_factor_library_catalog,
)


def _write_factor(
    zoo_root: Path,
    zoo: str,
    filename: str,
    factor_id: str,
    *,
    fields: tuple[str, ...] = ("close",),
    theme: tuple[str, ...] = ("momentum",),
    warmup: int = 5,
    extra: str = "",
) -> Path:
    zoo_dir = zoo_root / zoo
    zoo_dir.mkdir(parents=True, exist_ok=True)
    path = zoo_dir / filename
    path.write_text(
        textwrap.dedent(
            f"""
            __alpha_meta__ = {{
                "id": "{factor_id}",
                "theme": {list(theme)!r},
                "formula_latex": "metadata only",
                "columns_required": {list(fields)!r},
                "universe": ["equity_cn"],
                "frequency": ["1d"],
                "decay_horizon": 5,
                "min_warmup_bars": {warmup},
            }}

            {extra}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return path


def test_bundled_catalog_counts_are_build_results() -> None:
    catalog = build_factor_library_catalog()

    assert catalog.total_records == 461
    assert catalog.counts_by_zoo == {
        "academic": 11,
        "alpha101": 101,
        "fundamental": 4,
        "gtja191": 191,
        "qlib158": 154,
    }
    assert catalog.counts_by_status == {"METADATA_VALIDATED": 461}
    assert all(
        record.qualification_status is FactorQualificationStatus.METADATA_VALIDATED
        for record in catalog.records
    )


def test_catalog_hash_is_stable_for_same_source_tree(tmp_path: Path) -> None:
    zoo_root = tmp_path / "zoo"
    _write_factor(zoo_root, "mini", "alpha_001.py", "mini_001")
    _write_factor(zoo_root, "mini", "alpha_002.py", "mini_002", fields=("fund:roe",), theme=("quality",), warmup=1)

    first = FactorLibraryCatalogBuilder(zoo_root).build()
    second = FactorLibraryCatalogBuilder(zoo_root).build()

    assert first.to_dict() == second.to_dict()
    assert first.catalog_hash == second.catalog_hash
    assert "generated_at" not in first.to_dict()


def test_source_change_changes_record_and_catalog_hash(tmp_path: Path) -> None:
    zoo_root = tmp_path / "zoo"
    factor_path = _write_factor(zoo_root, "mini", "alpha_001.py", "mini_001")
    first = FactorLibraryCatalogBuilder(zoo_root).build()
    first_record = first.records[0]

    factor_path.write_text(
        factor_path.read_text(encoding="utf-8").replace('"min_warmup_bars": 5', '"min_warmup_bars": 6'),
        encoding="utf-8",
    )
    second = FactorLibraryCatalogBuilder(zoo_root).build()
    second_record = second.records[0]

    assert second_record.source_hash != first_record.source_hash
    assert second_record.metadata_hash != first_record.metadata_hash
    assert second.catalog_hash != first.catalog_hash


def test_corrupt_source_fails_closed(tmp_path: Path) -> None:
    zoo_root = tmp_path / "zoo"
    bad_dir = zoo_root / "mini"
    bad_dir.mkdir(parents=True)
    (bad_dir / "alpha_001.py").write_text("__alpha_meta__ = {\n", encoding="utf-8")

    with pytest.raises(CatalogBuildError) as exc:
        FactorLibraryCatalogBuilder(zoo_root).build()

    assert exc.value.code is CatalogFailureCode.MALFORMED_SOURCE


def test_duplicate_factor_id_fails_closed(tmp_path: Path) -> None:
    zoo_root = tmp_path / "zoo"
    _write_factor(zoo_root, "mini", "alpha_001.py", "mini_001")
    _write_factor(zoo_root, "mini", "alpha_002.py", "mini_001")

    with pytest.raises(CatalogBuildError) as exc:
        FactorLibraryCatalogBuilder(zoo_root).build()

    assert exc.value.code is CatalogFailureCode.DUPLICATE_FACTOR_ID


def test_path_traversal_source_path_fails_closed(tmp_path: Path) -> None:
    zoo_root = tmp_path / "zoo"
    _write_factor(zoo_root, "mini", "alpha_001.py", "mini_001")
    outside = tmp_path / "outside.py"
    outside.write_text(
        textwrap.dedent(
            """
            __alpha_meta__ = {
                "id": "mini_999",
                "theme": ["momentum"],
                "formula_latex": "close",
                "columns_required": ["close"],
                "universe": ["equity_cn"],
                "frequency": ["1d"],
                "decay_horizon": 5,
                "min_warmup_bars": 1,
            }
            """
        ).strip(),
        encoding="utf-8",
    )

    with pytest.raises(CatalogBuildError) as exc:
        FactorLibraryCatalogBuilder(zoo_root).build(paths=(outside,))

    assert exc.value.code is CatalogFailureCode.PATH_TRAVERSAL


def test_static_catalog_does_not_import_or_expose_compute(tmp_path: Path) -> None:
    zoo_root = tmp_path / "zoo"
    _write_factor(
        zoo_root,
        "mini",
        "alpha_001.py",
        "mini_001",
        extra="""
        def compute(panel):
            raise RuntimeError("this must never be imported or called")
        """,
    )

    catalog = FactorLibraryCatalogBuilder(zoo_root).build()

    assert catalog.total_records == 1
    rendered = str(catalog.to_dict())
    assert "compute" not in rendered
