from __future__ import annotations

from collections import Counter
import json
import os
from pathlib import Path
import tempfile
from typing import Iterable

from .models import QualificationInput, QualificationRun, QualificationTask, TaskStatus


_EVIDENCE_LAYERS = (
    "ast_ir_identity",
    "operator_policy",
    "whole_formula_reference",
    "pyqlib_0_9_7_parity",
    "frozen_pit_execution",
    "factor_score_artifact",
    "trial_ledger",
    "official_evidence_replay",
)


def schedule(records: Iterable[QualificationInput], *, projection_hash: str) -> QualificationRun:
    """Produce a deterministic, non-promoting work queue from frozen inputs."""

    inputs = tuple(sorted(records, key=lambda item: item.factor_id))
    counts = Counter(item.qualification for item in inputs)
    tasks: list[QualificationTask] = []
    for item in inputs:
        if item.qualification == "EXECUTION_READY":
            tasks.append(QualificationTask(item.factor_id, item.family, TaskStatus.STATIC_READY, (), ()))
            continue
        blockers = tuple(sorted(set(item.reason_codes) | set(item.missing_fields) | set(item.pit_missing_fields)))
        if item.qualification == "SEMANTICS_REVIEW_REQUIRED" and not item.missing_fields and not item.pit_missing_fields:
            tasks.append(QualificationTask(item.factor_id, item.family, TaskStatus.QUEUED, _EVIDENCE_LAYERS, blockers))
        else:
            tasks.append(QualificationTask(item.factor_id, item.family, TaskStatus.BLOCKED, (), blockers))
    return QualificationRun(projection_hash, dict(counts), tuple(tasks))


def write_run(run: QualificationRun, output: Path) -> Path:
    """Atomically materialize an audit snapshot; no source, data, or finance execution occurs."""

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "run.json"
    payload = json.dumps(run.payload(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=output, prefix=".run.", suffix=".tmp") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, target)
    return target
