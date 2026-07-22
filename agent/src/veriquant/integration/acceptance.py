"""Fail-closed integration acceptance for Prompt 11--18 capability packages."""

from __future__ import annotations

import json
import importlib.util
import os
import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping


ACCEPTANCE_STATUS_VALUES = (
    "IMPLEMENTED_AND_TESTED",
    "IMPLEMENTED_NOT_RUN",
    "UNAVAILABLE",
    "DEFERRED",
    "FAILED",
)

CAPABILITY_PROMPTS: dict[str, dict[str, str]] = {
    "prompt_11": {
        "agent": "Agent A",
        "package_id": "real_research",
        "connection": "Prompt 10 Frozen Snapshot -> Real Research Bridge",
    },
    "prompt_12": {
        "agent": "Agent B",
        "package_id": "statistical_promotion",
        "connection": "Evidence Service -> Statistical Promotion",
    },
    "prompt_13": {
        "agent": "Agent C",
        "package_id": "llm_desks",
        "connection": "Research DAG / FactorDAG -> Bounded LLM Desks",
    },
    "prompt_14": {
        "agent": "Agent D",
        "package_id": "research_memory",
        "connection": "Source-backed DRAFT -> Proposal Adapter -> FactorDAG/PIT/Evidence",
    },
    "prompt_17": {
        "agent": "Agent E",
        "package_id": "shadow_portfolio",
        "connection": "Statistical CHALLENGER -> Shadow Portfolio",
    },
    "prompt_18": {
        "agent": "Agent F",
        "package_id": "workbench",
        "connection": "Agent A-E artifacts -> Workbench ArtifactRepository",
    },
}

P11_P18_PACKAGE_IDS = frozenset(
    spec["package_id"] for spec in CAPABILITY_PROMPTS.values()
)

_PACKAGE_OWNED_PREFIXES: dict[str, tuple[str, ...]] = {
    "real_research": (
        "agent/src/veriquant/real_research/",
        "agent/tests/veriquant/parallel_real_research/",
        "docs/veriquant/parallel/REAL_RESEARCH_BRIDGE.md",
        ".autobuild/veriquant/parallel/real_research/",
    ),
    "statistical_promotion": (
        "agent/src/veriquant/statistical_promotion/",
        "agent/tests/veriquant/parallel_statistical_promotion/",
        "docs/veriquant/parallel/STATISTICAL_PROMOTION.md",
        ".autobuild/veriquant/parallel/statistical_promotion/",
    ),
    "llm_desks": (
        "agent/src/veriquant/llm_desks/",
        "agent/tests/veriquant/parallel_llm_desks/",
        "docs/veriquant/parallel/BOUNDED_LLM_DESKS.md",
        "docs/veriquant/parallel/LLM_DESKS_THREAT_MODEL.md",
        ".autobuild/veriquant/parallel/llm_desks/",
    ),
    "research_memory": (
        "agent/src/veriquant/research_memory/",
        "agent/tests/veriquant/parallel_research_memory/",
        "docs/veriquant/parallel/EVIDENCE_GROUNDED_RESEARCH_MEMORY.md",
        "docs/veriquant/parallel/DOCUMENT_PROVENANCE.md",
        "docs/veriquant/parallel/RESEARCH_MEMORY_POLICY.md",
        "docs/veriquant/parallel/LLM_DOCUMENT_THREAT_MODEL.md",
        ".autobuild/veriquant/parallel/research_memory/",
    ),
    "shadow_portfolio": (
        "agent/src/veriquant/shadow_portfolio/",
        "agent/tests/veriquant/parallel_shadow_portfolio/",
        "docs/veriquant/parallel/SHADOW_PORTFOLIO.md",
        ".autobuild/veriquant/parallel/shadow_portfolio/",
    ),
    "workbench": (
        "agent/src/api/veriquant_workbench_routes.py",
        "agent/tests/api/parallel_veriquant_workbench/",
        "frontend/src/pages/VeriquantWorkbench.tsx",
        "frontend/src/pages/__tests__/VeriquantWorkbench.test.tsx",
        "frontend/src/components/veriquant/",
        "docs/veriquant/parallel/VERIQUANT_WORKBENCH.md",
        ".autobuild/veriquant/parallel/workbench/",
    ),
}

_FORBIDDEN_ARTIFACT_PARTS = {"", ".", ".."}
_SECRET_PATTERNS = (
    re.compile(r"authorization\s*:\s*bearer\s+\S+", re.IGNORECASE),
    re.compile(r"\b(api[_-]?key|token|cookie|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_SEALED_KEY_RE = re.compile(r"(sealed|private_promotion|test_metric|test_rank|holdout)", re.IGNORECASE)
_SEALED_VALUE_RE = re.compile(
    r"\bsealed[_\-/](?:metric|score|rank|path)[A-Za-z0-9_\-/]*\b"
    r"|\bsealed\s+(?:metric|score|rank|path)\s*[:=]"
    r"|\bprivate_promotion(?:_reason)?\b",
    re.IGNORECASE,
)


class IntegrationAcceptanceError(ValueError):
    """Typed fail-closed integration error."""


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(payload: Any) -> str:
    return "sha256:" + sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, "MISSING"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - exact parser messages vary.
        return None, f"INVALID_JSON:{exc.__class__.__name__}"
    if not isinstance(payload, dict):
        return None, "INVALID_JSON:ROOT_NOT_OBJECT"
    return payload, None


def safe_artifact_relative_path(repo_root: str | Path, value: str) -> str:
    """Return a repo-relative path or raise on traversal/outside paths."""

    if not isinstance(value, str) or not value.strip():
        raise IntegrationAcceptanceError("artifact path must be a non-empty string")
    raw = value.replace("\\", "/")
    if "\x00" in raw:
        raise IntegrationAcceptanceError("artifact path contains a NUL byte")
    candidate = Path(raw)
    repo = Path(repo_root).resolve()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        try:
            return resolved.relative_to(repo).as_posix()
        except ValueError as exc:
            raise IntegrationAcceptanceError("artifact path escapes repository") from exc
    parts = candidate.parts
    if any(part in _FORBIDDEN_ARTIFACT_PARTS for part in parts):
        raise IntegrationAcceptanceError("artifact path contains traversal or empty segment")
    resolved = (repo / candidate).resolve()
    try:
        return resolved.relative_to(repo).as_posix()
    except ValueError as exc:
        raise IntegrationAcceptanceError("artifact path escapes repository") from exc


def redacted_for_workbench(value: Any) -> Any:
    """Return a sealed-safe projection for optional Workbench artifact views."""

    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _SEALED_KEY_RE.search(text_key):
                projected[text_key] = "[REDACTED_SEALED]"
            else:
                projected[text_key] = redacted_for_workbench(item)
        return projected
    if isinstance(value, list):
        return [redacted_for_workbench(item) for item in value]
    if isinstance(value, tuple):
        return [redacted_for_workbench(item) for item in value]
    if isinstance(value, str):
        if _SEALED_VALUE_RE.search(value):
            return "[REDACTED_SEALED]"
        rendered = value
        for pattern in _SECRET_PATTERNS:
            rendered = pattern.sub("[REDACTED_SECRET]", rendered)
        return rendered
    return value


def scan_text_for_forbidden_payload(text: str) -> tuple[str, ...]:
    findings: list[str] = []
    if _SEALED_VALUE_RE.search(text):
        findings.append("SEALED_REFERENCE")
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.append("SECRET_PATTERN")
            break
    if re.search(r"\b(eval|exec)\s*\(", text):
        findings.append("EXECUTABLE_EVAL_SURFACE")
    return tuple(sorted(set(findings)))


@dataclass(frozen=True)
class CapabilityPackage:
    package_id: str
    agent_id: str | None
    package_dir: str
    result_status: str
    eligible: bool
    blockers: tuple[str, ...]
    declared_blockers: tuple[str, ...]
    owned_files_changed: tuple[str, ...]
    tests_run: tuple[str, ...]
    test_result: str
    exported_symbols: tuple[str, ...]
    integration_hooks: tuple[str, ...]
    security_notes: tuple[str, ...]
    result_hash: str | None
    integration_request_hash: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "agent_id": self.agent_id,
            "package_dir": self.package_dir,
            "result_status": self.result_status,
            "eligible": self.eligible,
            "blockers": list(self.blockers),
            "declared_blockers": list(self.declared_blockers),
            "owned_files_changed": list(self.owned_files_changed),
            "tests_run": list(self.tests_run),
            "test_result": self.test_result,
            "exported_symbols": list(self.exported_symbols),
            "integration_hooks": list(self.integration_hooks),
            "security_notes": list(self.security_notes),
            "result_hash": self.result_hash,
            "integration_request_hash": self.integration_request_hash,
        }


def _tuple_of_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        return (_canonical_json(value),)
    if not isinstance(value, list):
        return ()
    rendered: list[str] = []
    for item in value:
        if isinstance(item, str):
            rendered.append(item)
        elif isinstance(item, Mapping):
            command = item.get("command")
            result = item.get("result")
            if isinstance(command, str) and isinstance(result, str):
                rendered.append(f"{command} => {result}")
            elif isinstance(command, str):
                rendered.append(command)
            else:
                rendered.append(_canonical_json(item))
    return tuple(rendered)


def _owned_path_allowed(package_id: str, path: str) -> bool:
    prefixes = _PACKAGE_OWNED_PREFIXES.get(package_id, ())
    return any(path == prefix or path.startswith(prefix) for prefix in prefixes)


def _test_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "UNKNOWN"
    return _canonical_json(value)


def _load_package(repo_root: Path, package_dir: Path) -> CapabilityPackage:
    result, result_error = _read_json(package_dir / "result.json")
    request, request_error = _read_json(package_dir / "integration_requests.json")
    blockers: list[str] = []
    if result_error:
        blockers.append(f"result.json:{result_error}")
        return CapabilityPackage(
            package_id=package_dir.name,
            agent_id=None,
            package_dir=safe_artifact_relative_path(repo_root, str(package_dir)),
            result_status="MISSING_RESULT",
            eligible=False,
            blockers=tuple(blockers),
            declared_blockers=(),
            owned_files_changed=(),
            tests_run=(),
            test_result="NOT_RUN",
            exported_symbols=(),
            integration_hooks=(),
            security_notes=(),
            result_hash=None,
            integration_request_hash=None,
        )

    assert result is not None
    package_id = package_dir.name
    agent_id = str(result.get("agent_id") or package_dir.name)
    owned_files = _tuple_of_strings(result.get("owned_files_changed"))
    normalized_owned: list[str] = []
    for item in owned_files:
        try:
            normalized = safe_artifact_relative_path(repo_root, item)
            normalized_owned.append(normalized)
            if not _owned_path_allowed(package_id, normalized):
                blockers.append(f"owned_files_changed:{normalized}:OUTSIDE_OWNED_PATHS")
            elif not (repo_root / normalized).exists():
                blockers.append(f"owned_files_changed:{normalized}:MISSING_ON_DISK")
        except IntegrationAcceptanceError as exc:
            blockers.append(f"owned_files_changed:{item}:{exc}")
    tests_run = _tuple_of_strings(result.get("tests_run"))
    exported = _tuple_of_strings(result.get("exported_symbols"))
    hooks = _tuple_of_strings(result.get("integration_hooks"))
    security = _tuple_of_strings(result.get("security_notes"))
    declared_blockers = _tuple_of_strings(result.get("blockers"))
    rendered = _canonical_json(result)
    findings = scan_text_for_forbidden_payload(rendered)
    blockers.extend(f"result.json:{finding}" for finding in findings)
    if request_error and request_error != "MISSING":
        blockers.append(f"integration_requests.json:{request_error}")
    if request:
        request_findings = scan_text_for_forbidden_payload(_canonical_json(request))
        blockers.extend(f"integration_requests.json:{finding}" for finding in request_findings)

    status = str(result.get("status") or "UNKNOWN")
    test_result = _test_result_text(result.get("test_result"))
    if status != "READY_FOR_INTEGRATION":
        blockers.append(f"status:{status}")
    if not tests_run:
        blockers.append("tests_run:MISSING")
    if "pass" not in test_result.lower() and "passed" not in test_result.lower():
        blockers.append(f"test_result:{test_result}")
    if not hooks:
        blockers.append("integration_hooks:MISSING")

    return CapabilityPackage(
        package_id=package_id,
        agent_id=agent_id,
        package_dir=safe_artifact_relative_path(repo_root, str(package_dir)),
        result_status=status,
        eligible=not blockers,
        blockers=tuple(blockers),
        declared_blockers=declared_blockers,
        owned_files_changed=tuple(normalized_owned),
        tests_run=tests_run,
        test_result=test_result,
        exported_symbols=exported,
        integration_hooks=hooks,
        security_notes=security,
        result_hash=_hash(result),
        integration_request_hash=_hash(request) if request is not None else None,
    )


def discover_parallel_packages(repo_root: str | Path) -> tuple[CapabilityPackage, ...]:
    repo = Path(repo_root).resolve()
    parallel = repo / ".autobuild" / "veriquant" / "parallel"
    if not parallel.exists():
        return ()
    packages = [
        _load_package(repo, path)
        for path in sorted(parallel.iterdir())
        if path.is_dir() and path.name in P11_P18_PACKAGE_IDS
    ]
    return tuple(packages)


def probe_optional_capabilities() -> dict[str, Any]:
    """Return boolean-only availability facts without exposing credentials."""

    network_enabled = os.environ.get("ENABLE_ASHARE_NETWORK") == "1"
    qlib_enabled = os.environ.get("ENABLE_QLIB_ADAPTER") == "1"
    llm_enabled = os.environ.get("ENABLE_LLM_RESEARCH") == "1"
    tushare_credential = bool(os.environ.get("TUSHARE_TOKEN") or os.environ.get("TUSHARE_API_KEY"))
    qlib_python = os.environ.get("QLIB_PYTHON", "")
    qlib_python_exists = bool(qlib_python and Path(qlib_python).is_absolute() and Path(qlib_python).is_file())
    tushare_module = importlib.util.find_spec("tushare") is not None
    pyqlib_module = importlib.util.find_spec("qlib") is not None
    return {
        "offline_default": not network_enabled and not qlib_enabled and not llm_enabled,
        "deterministic_offline_ready": True,
        "network_enabled": network_enabled,
        "llm_provider_enabled": llm_enabled,
        "qlib_adapter_enabled": qlib_enabled,
        "qlib_python_configured": bool(qlib_python),
        "qlib_python_exists": qlib_python_exists,
        "pyqlib_module_available": pyqlib_module,
        "tushare_module_available": tushare_module,
        "tushare_credential_configured": tushare_credential,
        "tushare_runtime_ready": network_enabled and tushare_module and tushare_credential,
        "qlib_runtime_ready": qlib_enabled and (pyqlib_module or qlib_python_exists),
        "vibe_agentloop_available": (Path(__file__).resolve().parents[2] / "agent" / "loop.py").is_file(),
        "vibe_agentloop_required": False,
        "automatic_download_attempted": False,
        "automatic_install_attempted": False,
        "real_broker_enabled": False,
    }


def _availability() -> dict[str, Any]:
    return probe_optional_capabilities()


def build_acceptance_result(repo_root: str | Path) -> dict[str, Any]:
    repo = Path(repo_root).resolve()
    packages = discover_parallel_packages(repo)
    ready_by_id = {pkg.package_id: pkg for pkg in packages if pkg.eligible}
    package_dirs = {pkg.package_id: pkg for pkg in packages}
    prompt_statuses: dict[str, dict[str, Any]] = {}
    connection_status: dict[str, dict[str, Any]] = {}

    for prompt_id, spec in CAPABILITY_PROMPTS.items():
        package_id = spec["package_id"]
        ready = ready_by_id.get(package_id)
        existing = package_dirs.get(package_id)
        if ready is not None:
            status = "IMPLEMENTED_NOT_RUN"
            reason = "package is READY_FOR_INTEGRATION but this acceptance run did not wire it into public routes"
        elif existing is not None:
            status = "UNAVAILABLE"
            reason = "package directory exists but has no eligible READY_FOR_INTEGRATION result"
        else:
            status = "UNAVAILABLE"
            reason = "package result.json is absent"
        prompt_statuses[prompt_id] = {
            "agent": spec["agent"],
            "package_id": package_id,
            "implemented": status,
            "tested": status if status == "IMPLEMENTED_NOT_RUN" else "UNAVAILABLE",
            "smoke": "UNAVAILABLE",
            "reason": reason,
        }
        connection_status[spec["connection"]] = {
            "status": "DISABLED" if ready is None else "READY_NOT_WIRED",
            "package_id": package_id,
            "reason": reason,
        }

    prompt_statuses["prompt_15"] = {
        "agent": "Prompt 15",
        "package_id": "scheduler_or_live_ops",
        "implemented": "DEFERRED",
        "tested": "DEFERRED",
        "smoke": "DEFERRED",
        "reason": "Prompt 15 is explicitly out of scope and remains disabled",
    }

    blockers: list[str] = []
    if not ready_by_id:
        blockers.append("No eligible READY_FOR_INTEGRATION package was found under .autobuild/veriquant/parallel")
    blockers.extend(f"{pkg.package_id}:{';'.join(pkg.blockers)}" for pkg in packages if pkg.blockers)
    no_ready = not ready_by_id
    return {
        "schema_version": "veriquant-integration-acceptance/v1",
        "status": "FAILED" if no_ready else "IMPLEMENTED_NOT_RUN",
        "reason": "fail-closed: no capability package is eligible for public wiring" if no_ready else "ready packages require explicit wiring pass",
        "repo_root": str(repo),
        "capability_packages": [pkg.to_dict() for pkg in packages],
        "prompt_statuses": prompt_statuses,
        "connections": connection_status,
        "availability": _availability(),
        "security": {
            "sealed_redaction": "ENFORCED_BY_REDACTED_FOR_WORKBENCH",
            "path_traversal": "REJECTED_BY_SAFE_ARTIFACT_RELATIVE_PATH",
            "secrets_output": "BOOLEAN_PROBES_ONLY",
            "live_broker": "DISABLED",
            "llm_authority": "DRAFT_ONLY_WHEN_PACKAGE_READY",
        },
        "blockers": blockers,
        "artifact_hash": None,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    tmp_name = ""
    try:
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        tmp_name = handle.name
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(tmp_name, path)
    finally:
        if handle is not None:
            handle.close()
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_acceptance_result(repo_root: str | Path, output_path: str | Path | None = None) -> dict[str, Any]:
    repo = Path(repo_root).resolve()
    result = build_acceptance_result(repo)
    final_hash = _hash({key: value for key, value in result.items() if key != "artifact_hash"})
    result["artifact_hash"] = final_hash
    target = (
        Path(output_path)
        if output_path is not None
        else repo / ".autobuild" / "veriquant" / "integration" / "acceptance_result.json"
    )
    _atomic_write_text(target, _canonical_json(result) + "\n")
    parsed = json.loads(target.read_text(encoding="utf-8"))
    if parsed.get("artifact_hash") != final_hash:
        raise IntegrationAcceptanceError("acceptance artifact hash validation failed")
    return parsed




