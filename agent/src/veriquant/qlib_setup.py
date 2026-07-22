"""Explicit, non-installing setup helpers for optional local A-share/Qlib use."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from .adapters.qlib_subprocess import SubprocessLocalQlibVisibleRunner
from .contracts import CanonicalContract
from .data.ashare import (
    AShareDataRequest,
    AShareFailure,
    FrozenSnapshotStore,
    FrozenTradingCalendarStore,
    freeze_sse_szse_calendar,
    VibeAshareSnapshotAdapter,
)
from .data.ashare.staging import QlibCsvStager, VisibleStagingSplit


def _datetime(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamps must include an explicit timezone")
    return parsed


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from exc


def _safe_json(value: object) -> str:
    if isinstance(value, CanonicalContract):
        value = value.to_dict()
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _token_configured() -> bool:
    try:
        from src.config.accessor import get_env_config

        value = get_env_config().data.tushare_token.strip()
        return bool(value and value != "your-tushare-token")
    except Exception:
        return False


def diagnose() -> tuple[dict[str, object], int]:
    configured_python = os.environ.get("QLIB_PYTHON", "")
    provider_value = os.environ.get("QLIB_PROVIDER_URI", "")
    provider_path = Path(provider_value) if provider_value else None
    provider_exists = bool(
        provider_path is not None and provider_path.is_absolute() and provider_path.is_dir()
    )
    provider_layout = {
        "calendar": False,
        "instruments": False,
        "features": False,
    }
    if provider_exists and provider_path is not None:
        provider_layout["calendar"] = (provider_path / "calendars" / "day.txt").is_file()
        provider_layout["instruments"] = (provider_path / "instruments" / "all.txt").is_file()
        feature_root = provider_path / "features"
    python_path = Path(configured_python) if configured_python else None
    worker = SubprocessLocalQlibVisibleRunner(python_executable=configured_python)
    placeholder_hash = "sha256:" + "0" * 64
    qlib_python_valid = bool(
        python_path is not None and python_path.is_absolute() and python_path.is_file()
    )
    result = {
        "command": "diagnose",
        "python_executable": sys.executable,
        "python_version": ".".join(str(item) for item in sys.version_info[:3]),
        "network_enabled": os.environ.get("ENABLE_ASHARE_NETWORK") == "1",
        "tushare_module_available": importlib.util.find_spec("tushare") is not None,
        "tushare_credential_configured": _token_configured(),
        "qlib_adapter_enabled": os.environ.get("ENABLE_QLIB_ADAPTER") == "1",
        "qlib_python_configured": bool(configured_python),
        "qlib_python_absolute_existing": qlib_python_valid,
        "qlib_provider_uri_configured": bool(provider_value),
        "qlib_provider_absolute_existing": provider_exists,
        "qlib_provider_layout": provider_layout,
        "pyqlib_available_in_qlib_python": (
            worker.pyqlib_available(placeholder_hash) if qlib_python_valid else False
        ),
        "automatic_install_attempted": False,
        "automatic_download_attempted": False,
    }
    return result, 0


def print_install_plan() -> tuple[dict[str, object], int]:
    return (
        {
            "command": "print-install-plan",
            "executed": False,
            "commands": [
                "conda create -n vibe-qlib python=3.11",
                "python -m venv <dedicated-qlib-environment>",
                "<dedicated-qlib-python> -m pip install pyqlib tushare exchange-calendars",
                "set QLIB_PYTHON=<absolute-dedicated-qlib-python>",
                "set ENABLE_QLIB_ADAPTER=1",
            ],
            "notes": [
                "Review and execute manually in a dedicated environment.",
                "No provider data is downloaded by VeriQuant.",
                "Use stage-from-vibe and build-provider only with explicit local paths.",
            ],
        },
        0,
    )


def freeze_calendar(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    calendar = freeze_sse_szse_calendar(
        start_date=args.start_date,
        end_date=args.end_date,
        source_calendar=args.source_calendar,
    )
    persisted = FrozenTradingCalendarStore(args.output).persist(calendar)
    return (
        {
            "command": "freeze-calendar",
            "status": "FROZEN",
            "calendar": calendar.to_dict(),
            "calendar_hash": calendar.artifact_hash,
            "session_content_hash": calendar.session_content_hash,
            "persisted": persisted.to_dict(),
            "network_used": False,
        },
        0,
    )

def stage_from_vibe(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    preflight = {
        "command": "stage-from-vibe",
        "source": args.source,
        "estimated_remote_requests": len(args.codes) + 1,
        "availability_policy": "daily-close-next-frozen-session-open/v1",
        "historical_universe_policy": "tushare-index-weight-as-of/v1",
        "network_flag_required": True,
        "trading_calendar_manifest_required": True,
        "trading_calendar_manifest_supplied": bool(getattr(args, "calendar_manifest", None)),
    }
    if os.environ.get("ENABLE_ASHARE_NETWORK") != "1":
        return (
            {
                **preflight,
                "status": "UNAVAILABLE",
                "failure_code": "ADAPTER_UNAVAILABLE",
                "message": "ENABLE_ASHARE_NETWORK=1 is required; no request was made",
            },
            2,
        )
    calendar_manifest = getattr(args, "calendar_manifest", None)
    if not calendar_manifest:
        return (
            {
                **preflight,
                "status": "UNAVAILABLE",
                "failure_code": "INCOMPLETE_RESULT",
                "message": "--calendar-manifest is required before any real A-share request is made",
            },
            2,
        )
    try:
        trading_calendar = FrozenTradingCalendarStore().load_manifest(calendar_manifest)
    except Exception as exc:
        return (
            {
                **preflight,
                "status": "UNAVAILABLE",
                "failure_code": "INCOMPLETE_RESULT",
                "message": f"frozen trading calendar unavailable: {type(exc).__name__}",
            },
            2,
        )
    output = Path(args.output).resolve()
    request = AShareDataRequest(
        dataset_id=args.dataset_id,
        codes=tuple(args.codes),
        index_code=args.index_code,
        start_time=args.start,
        end_time=args.end,
        frequency="1D",
        adjustment=args.adjustment,
        source=args.source,
        decision_time=args.decision_time,
        fields=tuple(args.fields),
        data_version=args.data_version,
        cache_policy="reuse-identical",
    )
    store = FrozenSnapshotStore(output / "snapshots")
    built = VibeAshareSnapshotAdapter(store=store, trading_calendar=trading_calendar).build(request)
    if isinstance(built, AShareFailure):
        return (
            {
                **preflight,
                "status": built.status,
                "failure_code": built.code.value,
                "message": built.message,
                "request_hash": request.artifact_hash,
            },
            3,
        )
    split = VisibleStagingSplit(
        train_start=args.train_start,
        train_end=args.train_end,
        validation_start=args.validation_start,
        validation_end=args.validation_end,
    )
    staged = QlibCsvStager(output / "qlib-staging").stage(snapshot=built, split=split)
    return (
        {
            **preflight,
            "status": "STAGED",
            "request_hash": request.artifact_hash,
            "snapshot_hash": built.snapshot_hash,
            "snapshot_dataset_card_hash": built.dataset_card.artifact_hash,
            "staging": staged.to_dict(),
        },
        0,
    )


def build_provider(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    python_value = os.environ.get("QLIB_PYTHON", "")
    python_path = Path(python_value)
    staging = Path(args.staging).resolve()
    target = Path(args.target).resolve()
    if os.environ.get("ENABLE_QLIB_ADAPTER") != "1":
        return {"built": False, "status": "UNAVAILABLE", "reason": "adapter disabled"}, 2
    if not python_path.is_absolute() or not python_path.is_file():
        return {"built": False, "status": "UNAVAILABLE", "reason": "QLIB_PYTHON invalid"}, 2
    if not staging.is_dir() or not (staging / "manifest.json").is_file():
        return {"built": False, "status": "UNAVAILABLE", "reason": "staging unavailable"}, 2
    if target.exists() and any(target.iterdir()):
        return {"built": False, "status": "BLOCKED", "reason": "target is not empty"}, 2
    builder = Path(__file__).parent / "adapters" / "qlib_provider_builder.py"
    command = [
        str(python_path),
        str(builder.resolve()),
        "--staging",
        str(staging),
        "--target",
        str(target),
        "--region",
        args.region,
    ]
    qlib_home = Path(__file__).resolve().parents[3] / ".autobuild" / "veriquant" / "qlib_home"
    qlib_home.mkdir(parents=True, exist_ok=True)
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "USERPROFILE": str(qlib_home),
    }
    for name in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"):
        if os.environ.get(name):
            environment[name] = os.environ[name]
    try:
        completed = subprocess.run(
            command,
            shell=False,
            check=False,
            capture_output=True,
            timeout=args.timeout,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return {"built": False, "status": "FAILED", "reason": "builder timeout"}, 3
    if len(completed.stdout) + len(completed.stderr) > 64 * 1024:
        return {"built": False, "status": "FAILED", "reason": "builder output oversized"}, 3
    if completed.returncode != 0:
        return {
            "built": False,
            "status": "FAILED",
            "reason": "fixed builder returned non-zero",
        }, 3
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {"built": False, "status": "FAILED", "reason": "builder output invalid"}, 3
    return result, 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("diagnose")
    sub.add_parser("print-install-plan")
    freeze = sub.add_parser("freeze-calendar")
    freeze.add_argument("--output", default=".autobuild/veriquant/calendars")
    freeze.add_argument("--start-date", type=_date, required=True)
    freeze.add_argument("--end-date", type=_date, required=True)
    freeze.add_argument("--source-calendar", default="XSHG")
    stage = sub.add_parser("stage-from-vibe")
    stage.add_argument("--output", required=True)
    stage.add_argument("--calendar-manifest")
    stage.add_argument("--source", choices=("tushare",), required=True)
    stage.add_argument("--dataset-id", default="ashare-tushare")
    stage.add_argument("--codes", nargs="+", required=True)
    stage.add_argument("--index-code", required=True)
    stage.add_argument("--start", type=_datetime, required=True)
    stage.add_argument("--end", type=_datetime, required=True)
    stage.add_argument("--decision-time", type=_datetime, required=True)
    stage.add_argument("--adjustment", choices=("raw", "qfq", "hfq"), default="raw")
    stage.add_argument(
        "--fields",
        nargs="+",
        choices=("open", "high", "low", "close", "volume", "amount"),
        default=("open", "high", "low", "close", "volume"),
    )
    stage.add_argument("--data-version", default="tushare-daily/v1")
    stage.add_argument("--train-start", type=_datetime, required=True)
    stage.add_argument("--train-end", type=_datetime, required=True)
    stage.add_argument("--validation-start", type=_datetime, required=True)
    stage.add_argument("--validation-end", type=_datetime, required=True)
    build = sub.add_parser("build-provider")
    build.add_argument("--staging", required=True)
    build.add_argument("--target", required=True)
    build.add_argument("--region", default="cn")
    build.add_argument("--timeout", type=float, default=120.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "diagnose":
        result, code = diagnose()
    elif args.command == "print-install-plan":
        result, code = print_install_plan()
    elif args.command == "freeze-calendar":
        result, code = freeze_calendar(args)
    elif args.command == "stage-from-vibe":
        print(_safe_json({"event": "preflight", "source": args.source, "estimated_remote_requests": len(args.codes) + 1, "availability_policy": "daily-close-next-frozen-session-open/v1", "historical_universe_policy": "tushare-index-weight-as-of/v1", "trading_calendar_manifest_required": True, "trading_calendar_manifest_supplied": bool(getattr(args, "calendar_manifest", None))}), flush=True)
        result, code = stage_from_vibe(args)
    else:
        result, code = build_provider(args)
    print(_safe_json(result))
    return code


if __name__ == "__main__":
    raise SystemExit(main())










