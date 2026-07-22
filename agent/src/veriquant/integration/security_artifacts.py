"""Generate machine-readable VeriQuant-only route and security artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import veriquant_api_server

from veriquant.integration.v1_security import run_veriquant_only_security_acceptance


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    name = handle.name
    try:
        json.dump(payload, handle, sort_keys=True, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(name):
            os.unlink(name)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[4]
    output = repo_root / ".autobuild" / "veriquant" / "integration" / "v1_tencent_stage"
    app = veriquant_api_server.create_app(
        artifact_root=repo_root / ".autobuild" / "veriquant" / "integration" / "workbench_artifacts"
    )
    manifest = veriquant_api_server.build_route_manifest(app)
    report = run_veriquant_only_security_acceptance(
        manifest=manifest,
        workbench_payload={"sealed_status": "PASSED_REDACTED", "evidence": {"authority": "OFFICIAL_REDACTED"}},
        source_texts={
            "agent/veriquant_api_server.py": Path(veriquant_api_server.__file__).read_text(encoding="utf-8"),
        },
    )
    _atomic_json(output / "route_authority_manifest.json", manifest.to_dict())
    _atomic_json(output / "security_probe_report.json", report.to_dict())
    print(json.dumps({"manifest_hash": manifest.artifact_hash, "probe_hash": report.artifact_hash, "passed": report.passed}, sort_keys=True))
    return 0 if report.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())

