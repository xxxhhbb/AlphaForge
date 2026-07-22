"""Fixed visible-only pyqlib subprocess worker; never imports user code."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path


_FORBIDDEN = ("sealed", "test", "official", "promotion", "token", "secret")
_MAX_INPUT_BYTES = 4 * 1024 * 1024
_PRICE_FEATURES = ("$open", "$high", "$low", "$close", "$volume")


def _load_pyqlib():
    script_dir = Path(__file__).resolve().parent
    sys.path[:] = [
        item
        for item in sys.path
        if not item or Path(item).resolve() != script_dir
    ]
    import qlib
    from qlib.data import D

    return qlib, D


def _has_forbidden_reference(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return True
            normalized = key.lower()
            if any(token in normalized for token in _FORBIDDEN):
                return True
            if _has_forbidden_reference(item):
                return True
        return False
    if isinstance(value, list):
        return any(_has_forbidden_reference(item) for item in value)
    if isinstance(value, str):
        normalized = value.lower()
        return normalized in _FORBIDDEN or any(normalized.startswith(token + ":") for token in _FORBIDDEN)
    return False

def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    result = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + 1 + end) / 2.0
        for index in order[cursor:end]:
            result[index] = rank
        cursor = end
    return result


def _pearson(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return 0.0 if denominator == 0.0 else numerator / denominator


def _metrics(rows: list[dict[str, object]]) -> dict[str, float]:
    scores = [float(row["score"]) for row in rows]
    labels = [float(row["label"]) for row in rows]
    if not all(math.isfinite(value) for value in (*scores, *labels)):
        raise ValueError("non-finite visible row")
    return {
        "pearson_ic": _pearson(scores, labels),
        "rank_ic": _pearson(_ranks(scores), _ranks(labels)),
        "observations": float(len(rows)),
    }


def _load(path: Path) -> dict[str, object]:
    if not path.is_file() or path.stat().st_size > _MAX_INPUT_BYTES:
        raise ValueError("worker input missing or oversized")
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    if _has_forbidden_reference(payload):
        raise ValueError("forbidden visible input token")
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "manifest",
        "candidate_hash",
        "rows",
    }:
        raise ValueError("worker input schema mismatch")
    if payload["schema_version"] != "veriquant.qlib-worker-input/v1":
        raise ValueError("worker input version mismatch")
    return payload


def _visible_provider_counts(manifest: dict[str, object], data_api: object) -> dict[str, float]:
    allowed = {
        "contract_type",
        "schema_version",
        "manifest_hash",
        "provider_uri",
        "region",
        "universe",
        "train",
        "validation",
        "label",
        "model",
        "strategy",
        "cost",
        "seed",
    }
    if set(manifest) != allowed:
        raise ValueError("visible manifest schema mismatch")
    universe = manifest.get("universe")
    if not isinstance(universe, list) or not universe or not all(isinstance(item, str) for item in universe):
        raise ValueError("visible universe is invalid")
    counts: dict[str, float] = {}
    for partition in ("train", "validation"):
        segment = manifest.get(partition)
        if not isinstance(segment, dict) or set(segment) != {"contract_type", "schema_version", "start", "end"}:
            raise ValueError("visible segment schema mismatch")
        start_raw = segment.get("start")
        end_raw = segment.get("end")
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            raise ValueError("visible segment timestamps are invalid")
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).date().isoformat()
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00")).date().isoformat()
        frame = data_api.features(list(universe), list(_PRICE_FEATURES), start_time=start, end_time=end, freq="day")
        counts[partition] = float(len(frame))
        if counts[partition] <= 0:
            raise ValueError("local provider has no visible rows")
    return counts


def _run(input_path: Path, output_path: Path) -> None:
    payload = _load(input_path)
    manifest = payload["manifest"]
    rows = payload["rows"]
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("worker manifest or rows are invalid")
    provider_uri = manifest.get("provider_uri")
    region = manifest.get("region")
    if not isinstance(provider_uri, str) or not Path(provider_uri).is_absolute():
        raise ValueError("provider URI must be an absolute local path")
    if not Path(provider_uri).is_dir() or not isinstance(region, str):
        raise ValueError("local provider is unavailable")
    qlib, data_api = _load_pyqlib()

    qlib.init(provider_uri=provider_uri, region=region)
    provider_counts = _visible_provider_counts(manifest, data_api)
    grouped: dict[str, list[dict[str, object]]] = {"train": [], "validation": []}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "contract_type",
            "schema_version",
            "partition",
            "event_time",
            "symbol",
            "score",
            "label",
        }:
            raise ValueError("visible row schema mismatch")
        partition = row.get("partition")
        if partition not in grouped:
            raise ValueError("non-visible partition")
        grouped[partition].append(row)
    if not all(grouped.values()):
        raise ValueError("both visible partitions are required")
    result = {name: _metrics(partition_rows) for name, partition_rows in grouped.items()}
    for name in ("train", "validation"):
        result[name]["provider_observations"] = provider_counts[name]
    output_path.write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--input")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.probe:
        try:
            _load_pyqlib()
        except Exception:
            return 2
        sys.stdout.write('{"pyqlib":true}')
        return 0
    try:
        if not args.input or not args.output:
            raise ValueError("input and output are required")
        _run(Path(args.input), Path(args.output))
        return 0
    except Exception as exc:
        sys.stderr.write(json.dumps({"error_type": type(exc).__name__}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

