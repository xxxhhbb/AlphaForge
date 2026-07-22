from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest

import src.veriquant.adapters.qlib_subprocess as boundary
from src.veriquant.adapters.qlib import (
    AdapterFailure,
    QlibCostConfig,
    QlibDateSegment,
    QlibManifest,
    QlibVisibleRow,
)
from src.veriquant.adapters.qlib_subprocess import SubprocessLocalQlibVisibleRunner
from src.veriquant.contracts import FailureCode
from src.veriquant.data import FieldRole
from src.veriquant.factors import parse_compile_factor_dag


UTC = timezone.utc


def at(day: int) -> datetime:
    return datetime(2024, 3, day, 16, 0, tzinfo=UTC)


def manifest(provider_uri: str) -> QlibManifest:
    return QlibManifest(
        provider_uri=provider_uri,
        region="cn",
        universe=("000001.SZ",),
        train=QlibDateSegment(start=at(1), end=at(2)),
        validation=QlibDateSegment(start=at(3), end=at(4)),
        test=QlibDateSegment(start=at(5), end=at(6)),
        label="future_return_5d",
        model="fixed-visible-diagnostics",
        strategy="auxiliary-only",
        cost=QlibCostConfig(
            open_cost_bps=5.0,
            close_cost_bps=15.0,
            linear_impact_bps=2.0,
            minimum_cost=5.0,
        ),
        seed=7,
    )


def candidate():
    return parse_compile_factor_dag(
        {
            "schema_version": "veriquant.factor-dag/v1",
            "dag_id": "visible-momentum",
            "nodes": [
                {"id": "close", "op": "field", "field": "close"},
                {"id": "momentum", "op": "pct_change", "inputs": ["close"], "periods": 20},
            ],
            "output_node": "momentum",
        },
        field_roles={"close": FieldRole.FEATURE},
    )


def rows() -> tuple[QlibVisibleRow, ...]:
    return (
        QlibVisibleRow(partition="train", event_time=at(1), symbol="000001.SZ", score=-1.0, label=-0.02),
        QlibVisibleRow(partition="train", event_time=at(2), symbol="000001.SZ", score=1.0, label=0.03),
        QlibVisibleRow(partition="validation", event_time=at(3), symbol="000001.SZ", score=-0.5, label=-0.01),
        QlibVisibleRow(partition="validation", event_time=at(4), symbol="000001.SZ", score=0.5, label=0.02),
    )


def install_fake_success(monkeypatch: pytest.MonkeyPatch, payload=None):
    calls = []
    output_payload = payload or {
        "train": {"pearson_ic": 1.0, "rank_ic": 1.0, "observations": 2.0},
        "validation": {"pearson_ic": 1.0, "rank_ic": 1.0, "observations": 2.0},
    }

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        output = command[command.index("--output") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(output_payload, handle)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(boundary.subprocess, "run", fake_run)
    return calls


def test_fixed_worker_uses_argument_list_no_shell_and_minimal_environment(tmp_path, monkeypatch) -> None:
    calls = install_fake_success(monkeypatch)
    runner = SubprocessLocalQlibVisibleRunner(python_executable=sys.executable)
    result = runner.run_visible(
        manifest=manifest(str(tmp_path)).visible(),
        candidate=candidate(),
        rows=rows(),
    )
    assert not isinstance(result, AdapterFailure)
    command, kwargs = calls[0]
    assert isinstance(command, list)
    assert command[0] == sys.executable
    assert command[1].endswith("qlib_worker.py")
    assert kwargs["shell"] is False
    assert "TUSHARE_TOKEN" not in kwargs["env"]
    assert set(result) == {"train", "validation"}


def test_timeout_nonzero_and_oversized_output_are_typed_failures(tmp_path, monkeypatch) -> None:
    runner = SubprocessLocalQlibVisibleRunner(python_executable=sys.executable, timeout_seconds=0.1)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(boundary.subprocess, "run", timeout)
    result = runner.run_visible(manifest=manifest(str(tmp_path)).visible(), candidate=candidate(), rows=rows())
    assert isinstance(result, AdapterFailure)
    assert result.code is FailureCode.ADAPTER_EXECUTION_FAILED
    assert result.retryable is True

    monkeypatch.setattr(
        boundary.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 9, stdout=b"", stderr=b"sensitive-detail"),
    )
    result = runner.run_visible(manifest=manifest(str(tmp_path)).visible(), candidate=candidate(), rows=rows())
    assert isinstance(result, AdapterFailure)
    assert "sensitive-detail" not in result.message

    monkeypatch.setattr(
        boundary.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=b"x" * (70 * 1024), stderr=b""),
    )
    result = runner.run_visible(manifest=manifest(str(tmp_path)).visible(), candidate=candidate(), rows=rows())
    assert isinstance(result, AdapterFailure)
    assert "output limit" in result.message


def test_invalid_or_forbidden_worker_payload_fails_closed(tmp_path, monkeypatch) -> None:
    calls = install_fake_success(
        monkeypatch,
        payload={
            "train": {"rank_ic": 1.0},
            "validation": {"sealed_rank_ic": 1.0},
        },
    )
    runner = SubprocessLocalQlibVisibleRunner(python_executable=sys.executable)
    result = runner.run_visible(manifest=manifest(str(tmp_path)).visible(), candidate=candidate(), rows=rows())
    assert calls
    assert isinstance(result, AdapterFailure)
    assert result.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_missing_or_relative_qlib_python_is_typed_unavailable(tmp_path) -> None:
    runner = SubprocessLocalQlibVisibleRunner(python_executable="python")
    result = runner.run_visible(manifest=manifest(str(tmp_path)).visible(), candidate=candidate(), rows=rows())
    assert isinstance(result, AdapterFailure)
    assert result.code is FailureCode.ADAPTER_UNAVAILABLE


def test_fixed_worker_input_contains_visible_manifest_only(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_run(command, **kwargs):
        input_path = command[command.index("--input") + 1]
        output_path = command[command.index("--output") + 1]
        captured.update(json.loads(open(input_path, encoding="utf-8").read()))
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump({"train": {"rank_ic": 0.1}, "validation": {"rank_ic": 0.2}}, handle)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(boundary.subprocess, "run", fake_run)
    result = SubprocessLocalQlibVisibleRunner(python_executable=sys.executable).run_visible(
        manifest=manifest(str(tmp_path)).visible(), candidate=candidate(), rows=rows()
    )
    assert not isinstance(result, AdapterFailure)
    rendered = json.dumps(captured, sort_keys=True).lower()
    assert '"test"' not in rendered
    assert "sealed" not in rendered
    assert "official" not in rendered
    assert set(row["partition"] for row in captured["rows"]) == {"train", "validation"}


def test_fixed_worker_queries_only_allowlisted_visible_provider_windows(tmp_path) -> None:
    class Data:
        def __init__(self):
            self.calls = []

        def features(self, instruments, fields, **kwargs):
            self.calls.append({"instruments": instruments, "fields": fields, **kwargs})
            return [1]

    from src.veriquant.adapters.qlib_worker import _visible_provider_counts

    data = Data()
    counts = _visible_provider_counts(manifest(str(tmp_path)).visible().to_dict(), data)
    assert counts == {"train": 1.0, "validation": 1.0}
    assert len(data.calls) == 2
    assert all(call["fields"] == ["$open", "$high", "$low", "$close", "$volume"] for call in data.calls)
    assert all(call["end_time"] <= "2024-03-04" for call in data.calls)
