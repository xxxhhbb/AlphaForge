"""Derive deterministic registered-baseline fields on one frozen PIT panel."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping

from ..data import MarketSnapshot, SnapshotRow, canonical_content_hash
from ..integration.research_os_v2 import FrozenValidationDataRepository
from ..research_os_v3.registered_baselines import build_registered_baselines
from ..research_toolbox.contracts import canonical_hash


SCHEMA_VERSION = "alphaforge.research500-registered-panel/v1"
RECEIPT_NAME = "registered_baselines_receipt.json"


def build_registered_panel(
    *, base_root: str | Path, output_root: str | Path, seed: int = 7
) -> Mapping[str, Any]:
    base = FrozenValidationDataRepository(Path(base_root).resolve()).load(
        require_strict_provenance=True
    )
    if base is None:
        raise ValueError("base frozen panel is unavailable")
    factors = build_registered_baselines(base, seed=seed)
    if len(factors) < 2:
        raise ValueError("registered baseline set is incomplete")
    factor_fields = tuple(item.factor_id for item in factors)
    if set(factor_fields) & set(base.snapshot.fields):
        raise ValueError("registered baseline field collides with source data")

    rows = []
    non_null = {name: 0 for name in factor_fields}
    by_factor = {item.factor_id: item for item in factors}
    for row in base.snapshot.rows:
        key = f"{row.event_time.isoformat()}|{row.symbol}"
        values = dict(row.values)
        for name in factor_fields:
            value = by_factor[name].scores.get(key)
            values[name] = value
            if value is not None:
                non_null[name] += 1
        rows.append(SnapshotRow(event_time=row.event_time, symbol=row.symbol, values=values))
    row_count = len(rows)
    coverage = dict(base.snapshot.coverage_by_field)
    coverage.update({name: non_null[name] / row_count for name in factor_fields})
    if any(coverage[name] <= 0.0 for name in factor_fields):
        raise ValueError("registered baseline has zero frozen coverage")
    snapshot = MarketSnapshot(
        dataset_id=base.snapshot.dataset_id + ".registered-baselines",
        decision_time=base.snapshot.decision_time,
        start_time=base.snapshot.start_time,
        fields=tuple(base.snapshot.fields) + factor_fields,
        rows=tuple(rows),
        historical_universe=base.snapshot.historical_universe,
        coverage_by_field=coverage,
    )
    qualification_hash = canonical_content_hash({
        "schema_version": SCHEMA_VERSION,
        "base_qualification_hash": base.qualification_hash,
        "base_snapshot_hash": base.snapshot.artifact_hash,
        "registered_factors": tuple(
            (item.factor_id, item.factor_hash, len(item.scores)) for item in factors
        ),
        "seed": seed,
    })
    derived = replace(base, snapshot=snapshot, qualification_hash=qualification_hash)
    output = Path(output_root).resolve()
    FrozenValidationDataRepository(output).save(derived)
    replay = FrozenValidationDataRepository(output).load(require_strict_provenance=True)
    if replay is None or replay.artifact_hash != derived.artifact_hash:
        raise ValueError("registered panel replay identity mismatch")
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": "PASSED",
        "base_data_hash": base.artifact_hash,
        "data_hash": derived.artifact_hash,
        "snapshot_hash": snapshot.artifact_hash,
        "qualification_hash": qualification_hash,
        "seed": seed,
        "factors": [
            {
                "factor_id": item.factor_id,
                "factor_hash": item.factor_hash,
                "field_name": item.factor_id,
                "score_count": len(item.scores),
                "coverage": coverage[item.factor_id],
            }
            for item in factors
        ],
        "replay_verified": True,
    }
    value["artifact_hash"] = canonical_hash(value)
    _atomic_json(output / RECEIPT_NAME, value)
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build registered baseline fields")
    parser.add_argument("--base-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    value = build_registered_panel(
        base_root=args.base_root, output_root=args.output_root, seed=args.seed
    )
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["RECEIPT_NAME", "SCHEMA_VERSION", "build_registered_panel"]
