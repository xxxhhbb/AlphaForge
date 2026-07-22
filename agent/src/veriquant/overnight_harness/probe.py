from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import Phase, SCHEMA_VERSION


def _check(phase: Phase, repo: Path) -> tuple[str, dict[str, object]]:
    if phase is Phase.DISCOVER:
        required = (
            "AGENT_CONTRIBUTOR_GUIDE.md",
            ".autobuild/veriquant/BUILD_CONSTITUTION.md",
            ".autobuild/veriquant/state.json",
            "docs/veriquant/QLIB_EXPRESSION_INTEGRATION_REPORT.md",
        )
        missing = [item for item in required if not (repo / item).is_file()]
        return ("READY" if not missing else "FAILED", {"missing": missing})
    if phase in {Phase.PLAN, Phase.IMPLEMENT}:
        from ..factor_ir_v2.policies import BUILTIN_ADAPTER_POLICIES
        from ..qlib_factor_library_projection import build_qlib_factor_library_projection

        projection = build_qlib_factor_library_projection()
        counts: dict[str, int] = {}
        for record in projection.records:
            key = record.execution_qualification.value
            counts[key] = counts.get(key, 0) + 1
        adapters = sorted(BUILTIN_ADAPTER_POLICIES)
        good = len(projection.records) == 461 and counts.get("EXECUTION_READY") == 112
        good = good and adapters == ["SafeIdxMax", "SafeIdxMin", "SafeIf", "SafeWMA"]
        return ("READY" if good else "FAILED", {"counts": counts, "adapters": adapters})
    if phase is Phase.INDEPENDENT_REVIEW:
        path = repo / ".autobuild/veriquant/independent_review/factorir_v2_final.json"
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        checks = payload.get("checks", {})
        good = payload.get("status") == "NO_GO" and checks.get("product_gate") == "NO_GO"
        return ("READY" if good else "FAILED", {"review_status": payload.get("status")})
    if phase is Phase.COMMIT_CHECKPOINT:
        required = tuple(
            repo / "docs/veriquant" / name
            for name in (
                "FACTOR_IR_V2_REPORT.md",
                "FACTOR_LIBRARY_461_STATUS.md",
                "OVERNIGHT_PRODUCT_REPORT.md",
                "OVERNIGHT_TEST_MATRIX.md",
                "OVERNIGHT_BLOCKERS.md",
            )
        )
        missing = [str(path) for path in required if not path.is_file()]
        return ("READY" if not missing else "FAILED", {"missing": missing})
    if phase is Phase.NEXT_PHASE:
        path = repo / ".autobuild/veriquant/integration/qlib_expression_migration/result.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        gate = payload.get("product_gate", {})
        return ("READY" if gate.get("status") == "GO" else "PRODUCT_GATE_NO_GO", dict(gate))
    return "READY", {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=[phase.value for phase in Phase])
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()
    phase = Phase(args.phase)
    status, evidence = _check(phase, args.repo_root.resolve())
    print(json.dumps({"schema_version": SCHEMA_VERSION, "phase": phase.value, "status": status, "evidence": evidence}, sort_keys=True))
    return 0 if status == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
