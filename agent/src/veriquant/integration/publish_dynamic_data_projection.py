"""Materialize a verified dynamic freeze as a Research OS data artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..dynamic_data_integration import enrich_frozen_validation_data
from .research_os_v2 import FrozenValidationDataRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish a PIT-safe dynamic-data projection"
    )
    parser.add_argument("--base-root", required=True)
    parser.add_argument("--freeze-root", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    base = FrozenValidationDataRepository(args.base_root).load()
    if base is None:
        raise ValueError("base frozen validation data is unavailable")
    enriched = enrich_frozen_validation_data(
        base,
        freeze_root=args.freeze_root,
    )
    repository = FrozenValidationDataRepository(args.output_root)
    artifact_hash = repository.save(enriched)
    print(json.dumps({
        "status": "SUCCESS",
        "source_label": enriched.source_label,
        "official_tushare": False,
        "dataset_id": enriched.snapshot.dataset_id,
        "field_count": len(enriched.snapshot.fields),
        "session_count": len(enriched.snapshot.historical_universe),
        "symbol_count": len({row.symbol for row in enriched.snapshot.rows}),
        "row_count": len(enriched.snapshot.rows),
        "artifact_hash": artifact_hash,
        "output_path": str(Path(repository.path).resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
