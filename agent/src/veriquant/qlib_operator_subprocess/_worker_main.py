"""Fixed worker entry point.  It accepts one bounded JSON request on stdin."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path


# ``python -I`` intentionally omits the repository from sys.path.  This fixed
# script, not caller input, derives the package root from its installed path.
_SRC_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SRC_ROOT))

from veriquant.qlib_operator_subprocess.models import (  # noqa: E402
    MAX_PROTOCOL_INPUT_BYTES,
    QlibSubprocessFailure,
    QlibSubprocessFailureCode,
)
from veriquant.qlib_operator_subprocess.wire import (  # noqa: E402
    WireFormatError,
    artifact_to_wire,
    dumps,
    loads,
    request_from_wire,
    subprocess_failure_to_wire,
    worker_failure_to_wire,
)
from veriquant.qlib_operator_worker.api import (  # noqa: E402
    QlibFactorScoreArtifact,
    execute_certified_qlib_operators,
)


_ZERO_HASH = "sha256:" + "0" * 64


def _deny_network(*_args: object, **_kwargs: object) -> object:
    raise PermissionError("network is disabled in the certified Qlib worker")


def _install_network_fence() -> None:
    socket.create_connection = _deny_network  # type: ignore[assignment]
    socket.getaddrinfo = _deny_network  # type: ignore[assignment]
    socket.socket.connect = _deny_network  # type: ignore[assignment]
    socket.socket.connect_ex = _deny_network  # type: ignore[assignment]


def _safe_hash(value: object) -> str:
    return value if isinstance(value, str) and value.startswith("sha256:") and len(value) == 71 else _ZERO_HASH


def _protocol_failure(raw: object, message: str) -> bytes:
    item = raw if isinstance(raw, dict) else {}
    program = item.get("program") if isinstance(item.get("program"), dict) else {}
    return dumps(
        subprocess_failure_to_wire(
            QlibSubprocessFailure(
                QlibSubprocessFailureCode.WORKER_PROTOCOL_FAILED,
                message[:512],
                _safe_hash(program.get("expression_hash")),
                _safe_hash(item.get("expected_input_hash")),
            )
        )
    )


def main() -> int:
    # Defense in depth: the launcher supplies a minimal environment and the
    # worker refuses provider/network convenience switches if injected.
    for forbidden in ("QLIB_PROVIDER_URI", "TUSHARE_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        os.environ.pop(forbidden, None)
    _install_network_fence()
    payload = sys.stdin.buffer.read(MAX_PROTOCOL_INPUT_BYTES + 1)
    if len(payload) > MAX_PROTOCOL_INPUT_BYTES:
        sys.stderr.write("input exceeds fixed worker protocol limit")
        return 2
    decoded: object = {}
    try:
        decoded = loads(payload)
        request, _policy = request_from_wire(decoded)
    except Exception:
        sys.stdout.buffer.write(_protocol_failure(decoded, "structured request failed closed"))
        return 0
    result = execute_certified_qlib_operators(request)
    wire = artifact_to_wire(result) if isinstance(result, QlibFactorScoreArtifact) else worker_failure_to_wire(result)
    sys.stdout.buffer.write(dumps(wire))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
