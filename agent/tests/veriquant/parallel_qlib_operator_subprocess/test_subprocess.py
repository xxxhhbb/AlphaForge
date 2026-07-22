from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.veriquant.qlib_expression.api import QlibExpressionPolicy, QlibFieldRole, certify_qlib_expression
from src.veriquant.qlib_operator_subprocess.api import (
    QlibOperatorSubprocessRunner,
    QlibSubprocessConfig,
    QlibSubprocessFailure,
    QlibSubprocessFailureCode,
)
from src.veriquant.qlib_operator_subprocess import runner as runner_module
from src.veriquant.qlib_operator_subprocess.runner import build_worker_environment
from src.veriquant.qlib_operator_subprocess.wire import (
    WireFormatError,
    dumps,
    node_from_wire,
    request_from_wire,
    request_to_wire,
)
from src.veriquant.qlib_operator_worker.api import QlibInMemoryFeatureRow, QlibOperatorExecutionRequest


H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64


def _policy() -> QlibExpressionPolicy:
    return QlibExpressionPolicy(
        field_roles={"close": QlibFieldRole.FEATURE, "volume": QlibFieldRole.FEATURE},
        max_window=20,
        max_ref_periods=20,
    )


def _request(expression: str = "Mean($close,3)") -> QlibOperatorExecutionRequest:
    program = certify_qlib_expression(expression, policy=_policy())
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = tuple(
        QlibInMemoryFeatureRow(
            start + timedelta(days=index),
            symbol,
            {field: base + index for field in program.required_fields},
        )
        for symbol, base in (("SH600000", 1.0), ("SZ000001", 11.0))
        for index in range(6)
    )
    return QlibOperatorExecutionRequest(program, H1, H2, ("SH600000", "SZ000001"), rows)


def _config(tmp_path: Path, **kwargs: object) -> QlibSubprocessConfig:
    return QlibSubprocessConfig.pin(
        python_path=Path(sys.executable).resolve(),
        sandbox_root=(tmp_path / "sandbox").resolve(),
        **kwargs,
    )


def test_wire_contains_structured_ast_not_raw_expression_and_roundtrips() -> None:
    request = _request()
    wire = request_to_wire(request, _policy())
    payload = dumps(wire)
    assert b"canonical_expression" not in payload
    assert b"Mean($close,3)" not in payload
    rebuilt, policy = request_from_wire(json.loads(payload))
    assert rebuilt.input_hash == request.input_hash
    assert rebuilt.program.expression_hash == request.program.expression_hash
    assert policy.policy_hash == _policy().policy_hash


def test_raw_expression_and_malicious_ast_fail_closed() -> None:
    with pytest.raises(WireFormatError):
        node_from_wire("Mean($close,3)")
    with pytest.raises(WireFormatError):
        node_from_wire({"type": "python", "code": "__import__('os').system('whoami')"})
    with pytest.raises(WireFormatError):
        node_from_wire({"type": "call", "name": "Mean", "arguments": [], "code": "x"})

    wire = request_to_wire(_request("Ref($close,2)"), _policy())
    wire["program"]["root"]["arguments"][1] = {"type": "unary", "operator": "-", "operand": {"type": "number", "value": "1"}}
    with pytest.raises(Exception):
        request_from_wire(wire)


def test_policy_is_revalidated_in_worker_wire() -> None:
    wire = request_to_wire(_request(), _policy())
    wire["policy"]["field_roles"]["close"] = "SEALED"
    with pytest.raises(WireFormatError, match="policy hash mismatch"):
        request_from_wire(wire)


def test_config_rejects_relative_python_and_detects_replacement(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        QlibSubprocessConfig(Path("python.exe"), H1, (tmp_path / "sandbox").resolve())
    config = replace(_config(tmp_path), python_sha256=H1)
    result = QlibOperatorSubprocessRunner(config).execute(_request(), policy=_policy())
    assert isinstance(result, QlibSubprocessFailure)
    assert result.code is QlibSubprocessFailureCode.PYTHON_NOT_PINNED


def test_worker_environment_is_allowlisted_and_drops_secrets(tmp_path: Path) -> None:
    environment = build_worker_environment(
        tmp_path.resolve(),
        {
            "SystemRoot": r"C:\Windows",
            "PATH": "attacker-path",
            "OPENAI_API_KEY": "secret",
            "TUSHARE_TOKEN": "secret",
            "QLIB_PROVIDER_URI": "https://example.invalid",
        },
    )
    assert environment["USERPROFILE"] == str(tmp_path.resolve())
    assert environment["SystemRoot"] == r"C:\Windows"
    for forbidden in ("PATH", "OPENAI_API_KEY", "TUSHARE_TOKEN", "QLIB_PROVIDER_URI"):
        assert forbidden not in environment


class _TimeoutProcess:
    returncode = None

    def communicate(self, _payload=None, timeout=None):
        if timeout is not None:
            raise subprocess.TimeoutExpired("worker", timeout)
        self.returncode = -9
        return b"", b""

    def kill(self):
        self.returncode = -9


class _OversizeProcess:
    returncode = 0

    def communicate(self, _payload=None, timeout=None):
        return b"x" * 129, b""


class _OversizeDiagnosticProcess:
    returncode = 0

    def communicate(self, _payload=None, timeout=None):
        return b"{}", b"x" * 129


def test_timeout_is_typed_and_process_is_killed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *args, **kwargs: _TimeoutProcess())
    result = QlibOperatorSubprocessRunner(_config(tmp_path, timeout_seconds=0.05)).execute(_request(), policy=_policy())
    assert isinstance(result, QlibSubprocessFailure)
    assert result.code is QlibSubprocessFailureCode.TIMEOUT
    assert result.retryable is True


def test_oversize_input_and_output_fail_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    input_result = QlibOperatorSubprocessRunner(_config(tmp_path, max_input_bytes=128)).execute(_request(), policy=_policy())
    assert isinstance(input_result, QlibSubprocessFailure)
    assert input_result.code is QlibSubprocessFailureCode.INPUT_TOO_LARGE

    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *args, **kwargs: _OversizeProcess())
    output_result = QlibOperatorSubprocessRunner(_config(tmp_path, max_output_bytes=128)).execute(_request(), policy=_policy())
    assert isinstance(output_result, QlibSubprocessFailure)
    assert output_result.code is QlibSubprocessFailureCode.OUTPUT_TOO_LARGE

    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *args, **kwargs: _OversizeDiagnosticProcess())
    diagnostic_result = QlibOperatorSubprocessRunner(_config(tmp_path, max_diagnostic_bytes=128)).execute(_request(), policy=_policy())
    assert isinstance(diagnostic_result, QlibSubprocessFailure)
    assert diagnostic_result.code is QlibSubprocessFailureCode.OUTPUT_TOO_LARGE


def test_fixed_worker_source_replacement_is_detected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner_module, "PINNED_WORKER_SOURCE_HASH", H1)
    result = QlibOperatorSubprocessRunner(_config(tmp_path)).execute(_request(), policy=_policy())
    assert isinstance(result, QlibSubprocessFailure)
    assert result.code is QlibSubprocessFailureCode.WORKER_IDENTITY_MISMATCH


def test_runner_rejects_arbitrary_string_and_fixed_command_has_no_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = QlibOperatorSubprocessRunner(_config(tmp_path))
    with pytest.raises(TypeError, match="expression strings are prohibited"):
        runner.execute("Mean($close,3)", policy=_policy())  # type: ignore[arg-type]

    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return _OversizeProcess()

    monkeypatch.setattr(runner_module.subprocess, "Popen", fake_popen)
    runner.execute(_request(), policy=_policy())
    assert captured["shell"] is False
    assert captured["command"][0] == str(Path(sys.executable).resolve())
    assert captured["command"][1:3] == ("-I", "-B")
    assert Path(captured["command"][3]) == runner_module.PINNED_WORKER_PATH
    assert "OPENAI_API_KEY" not in captured["env"]


def test_base_runtime_subprocess_returns_typed_unavailable_or_artifact(tmp_path: Path) -> None:
    result = QlibOperatorSubprocessRunner(_config(tmp_path)).execute(_request(), policy=_policy())
    # Base CI may or may not have the pinned optional runtime. Both outcomes
    # prove a real fixed subprocess and a typed, identity-bound response.
    assert getattr(result, "program_hash") == _request().program.expression_hash
    assert getattr(result, "input_hash") == _request().input_hash


def test_result_manifest_declares_verifiable_owned_artifact_hashes() -> None:
    root = Path(__file__).resolve().parents[4]
    result_path = root / ".autobuild" / "veriquant" / "parallel" / "qlib_operator_subprocess" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    artifact_hashes = result["artifact_hashes"]
    assert artifact_hashes
    for relative, declared in artifact_hashes.items():
        artifact = (root / relative).resolve()
        assert artifact.is_relative_to(root.resolve())
        assert artifact.is_file()
        actual = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert actual == declared
    assert result["identity_hashes"]["real_smoke_output_hash"].startswith("sha256:")
