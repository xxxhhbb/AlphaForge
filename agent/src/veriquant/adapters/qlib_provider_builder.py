"""Fixed-code conversion from VeriQuant CSV staging to a local Qlib day provider."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import struct
from pathlib import Path


_FIELDS = ("open", "high", "low", "close", "volume", "amount")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def build(staging: Path, target: Path, region: str) -> dict[str, object]:
    import qlib

    if not staging.is_dir() or not (staging / "manifest.json").is_file():
        raise ValueError("staging manifest is unavailable")
    if target.exists() and any(target.iterdir()):
        raise ValueError("provider target must be absent or empty")
    manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("partitions") != ["train", "validation"]:
        raise ValueError("only visible staging can build a provider")
    calendar = [
        line.strip()
        for line in (staging / "calendars" / "day.txt").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not calendar or len(set(calendar)) != len(calendar):
        raise ValueError("staging calendar is invalid")
    date_index = {day: index for index, day in enumerate(calendar)}
    target.mkdir(parents=True, exist_ok=True)
    _atomic_write(target / "calendars" / "day.txt", ("\n".join(calendar) + "\n").encode("utf-8"))
    instruments: list[str] = []
    feature_files = 0
    for csv_path in sorted((staging / "csv").glob("*.csv")):
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if not rows:
            continue
        if any(row.get("partition") not in {"train", "validation"} for row in rows):
            raise ValueError("non-visible partition in staged CSV")
        symbol = rows[0].get("instrument")
        if not symbol or any(row.get("instrument") != symbol for row in rows):
            raise ValueError("staged instrument is invalid")
        dates = [row.get("datetime", "") for row in rows]
        if any(day not in date_index for day in dates):
            raise ValueError("staged date is outside calendar")
        start, end = min(dates), max(dates)
        instruments.append(f"{symbol}\t{start}\t{end}")
        first_index = date_index[start]
        last_index = date_index[end]
        by_date = {row["datetime"]: row for row in rows}
        for field in _FIELDS:
            if field not in rows[0]:
                continue
            values: list[float] = []
            for day in calendar[first_index : last_index + 1]:
                raw = by_date.get(day, {}).get(field, "")
                values.append(float(raw) if raw not in (None, "") else math.nan)
            content = struct.pack("<f", float(first_index)) + struct.pack(
                f"<{len(values)}f", *values
            )
            _atomic_write(
                target / "features" / symbol.lower() / f"{field}.day.bin",
                content,
            )
            feature_files += 1
    if not instruments or feature_files == 0:
        raise ValueError("staging produced no provider features")
    _atomic_write(
        target / "instruments" / "all.txt",
        ("\n".join(sorted(instruments)) + "\n").encode("utf-8"),
    )
    qlib.init(provider_uri=str(target), region=region)
    return {
        "built": True,
        "provider_uri": str(target),
        "calendar_days": len(calendar),
        "instruments": len(instruments),
        "feature_files": feature_files,
        "source_staging_hash": manifest.get("staging_hash"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--region", default="cn")
    args = parser.parse_args(argv)
    try:
        result = build(Path(args.staging).resolve(), Path(args.target).resolve(), args.region)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(json.dumps({"built": False, "error_type": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
