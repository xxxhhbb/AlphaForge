"""Real pyqlib 0.9.7 subprocess smoke; intentionally independent of pytest."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.veriquant.qlib_expression.api import QlibExpressionPolicy, QlibFieldRole, certify_qlib_expression
from src.veriquant.qlib_operator_subprocess.api import QlibOperatorSubprocessRunner, QlibSubprocessConfig
from src.veriquant.qlib_operator_worker.api import QlibFactorScoreArtifact, QlibInMemoryFeatureRow, QlibOperatorExecutionRequest


H1 = "sha256:" + "a" * 64
H2 = "sha256:" + "b" * 64


def main() -> int:
    policy = QlibExpressionPolicy(
        field_roles={"close": QlibFieldRole.FEATURE, "volume": QlibFieldRole.FEATURE},
        max_window=20,
        max_ref_periods=20,
    )
    program = certify_qlib_expression("If($close>Mean($close,3),Corr($close,$volume,3),Ref($close,2))", policy=policy)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = tuple(
        QlibInMemoryFeatureRow(
            start + timedelta(days=index),
            symbol,
            {"close": base + index, "volume": (base + index) * 100.0},
        )
        for symbol, base in (("SH600000", 1.0), ("SZ000001", 11.0))
        for index in range(8)
    )
    request = QlibOperatorExecutionRequest(program, H1, H2, ("SH600000", "SZ000001"), rows)
    root = Path(__file__).resolve().parents[5]
    config = QlibSubprocessConfig.pin(
        python_path=Path(sys.executable).resolve(),
        sandbox_root=(root / ".autobuild" / "veriquant" / "parallel" / "qlib_operator_subprocess" / "sandbox").resolve(),
        timeout_seconds=90,
    )
    first = QlibOperatorSubprocessRunner(config).execute(request, policy=policy)
    second = QlibOperatorSubprocessRunner(config).execute(request, policy=policy)
    if not isinstance(first, QlibFactorScoreArtifact) or not isinstance(second, QlibFactorScoreArtifact):
        raise AssertionError(f"subprocess smoke failed: {first!r} / {second!r}")
    assert first.output_hash == second.output_hash
    assert first.rows == second.rows
    print(json.dumps({
        "status": "PASSED",
        "runtime": "pyqlib-0.9.7-dedicated-subprocess",
        "program_hash": first.program_hash,
        "input_hash": first.input_hash,
        "runtime_hash": first.runtime_hash,
        "output_hash": first.output_hash,
        "rows": len(first.rows),
        "replay_hash_equal": True,
        "raw_expression_on_wire": False,
        "shell": False,
        "network": False,
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
