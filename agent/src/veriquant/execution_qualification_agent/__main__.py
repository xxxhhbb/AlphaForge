from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from veriquant.formula_validation_factory import FormulaValidationFactory
from veriquant.qlib_factor_library_projection import build_qlib_factor_library_projection

from .agent import schedule, write_run
from .models import QualificationInput


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize the non-authoritative execution-qualification queue")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--matrix-output", type=Path)
    args = parser.parse_args()
    projection = build_qlib_factor_library_projection()
    records = tuple(
        QualificationInput(
            factor_id=item.factor_id,
            family=item.family,
            qualification=item.execution_qualification.value,
            reason_codes=item.reason_codes,
            missing_fields=item.missing_fields,
            pit_missing_fields=item.pit_missing_fields,
            static_union_member=item.static_union_member,
        )
        for item in projection.records
    )
    target = write_run(schedule(records, projection_hash=projection.projection_hash), args.output)
    matrix_output = (args.matrix_output or args.output / "matrices").resolve()
    matrix_output.mkdir(parents=True, exist_ok=True)
    factory = FormulaValidationFactory()
    planned = 0
    for record in projection.records:
        if record.execution_qualification.value not in {"EXECUTION_READY", "SEMANTICS_REVIEW_REQUIRED"}:
            continue
        matrix = factory.from_projection(record, projection_hash=projection.projection_hash)
        rendered = json.dumps(
            matrix.to_canonical() | {"matrix_hash": matrix.matrix_hash},
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ) + "\n"
        destination = matrix_output / f"{record.factor_id}.json"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=matrix_output, prefix=".matrix.", suffix=".tmp") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, destination)
        planned += 1
    print(f"matrices={planned}")
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
