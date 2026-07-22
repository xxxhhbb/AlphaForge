"""Mechanical, fail-closed checks for AlphaForge formal research closure."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar, Mapping

from ..contracts import CanonicalContract, FailureCode
from ..contracts.base import fail, freeze_mapping, require_hash, require_identifier


FORMAL_ACCEPTANCE_SCHEMA_VERSION = "alphaforge.formal-research-acceptance/v1"


class GateStatus(str, Enum):
    IMPLEMENTED_AND_TESTED = "IMPLEMENTED_AND_TESTED"
    IMPLEMENTED_NOT_RUN = "IMPLEMENTED_NOT_RUN"
    UNAVAILABLE = "UNAVAILABLE"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"


@dataclass(frozen=True, kw_only=True)
class FormalResearchAcceptanceVerdict(CanonicalContract):
    SCHEMA_VERSION: ClassVar[str] = FORMAL_ACCEPTANCE_SCHEMA_VERSION

    verdict_id: str
    gates: Mapping[str, GateStatus]
    evidence_hashes: Mapping[str, str]
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_identifier(self.verdict_id, "verdict_id")
        gates = freeze_mapping(self.gates, "gates")
        for key, value in gates.items():
            require_identifier(key, "gates.key")
            if not isinstance(value, GateStatus):
                gates = dict(gates)
                gates[key] = GateStatus(value)
        object.__setattr__(self, "gates", dict(sorted(gates.items())))
        hashes = freeze_mapping(self.evidence_hashes, "evidence_hashes")
        for key, value in hashes.items():
            require_identifier(key, "evidence_hashes.key")
            require_hash(value, "evidence_hashes.value")
        object.__setattr__(self, "evidence_hashes", dict(sorted(hashes.items())))

    @property
    def all_required_passed(self) -> bool:
        required = (
            "contracts",
            "real_data_binding",
            "official_evidence_bridge",
            "formal_report",
            "workbench_publication",
            "formal_backend_command",
        )
        return all(self.gates.get(name) is GateStatus.IMPLEMENTED_AND_TESTED for name in required)


class FormalResearchAcceptanceEvaluator:
    """Read package result files and produce a mechanical verdict."""

    REQUIRED_PACKAGES = {
        "contracts": "research_closure_contracts",
        "real_data_binding": "campaign_real_data_binding",
        "official_evidence_bridge": "campaign_official_evidence",
        "formal_report": "formal_report",
        "workbench_publication": "formal_report_publication",
        "formal_backend_command": "formal_research_integration",
    }

    def evaluate(self, *, repo_root: str | Path) -> FormalResearchAcceptanceVerdict:
        root = Path(repo_root).resolve()
        gates: dict[str, GateStatus] = {}
        hashes: dict[str, str] = {}
        blockers: list[str] = []
        for gate, package_id in self.REQUIRED_PACKAGES.items():
            path = root / ".autobuild/veriquant/parallel" / package_id / "result.json"
            payload = _read_json(path)
            if payload.get("status") == "READY_FOR_INTEGRATION" and payload.get("test_result"):
                gates[gate] = GateStatus.IMPLEMENTED_AND_TESTED
                hashes[gate] = _declared_hash(payload)
            else:
                gates[gate] = GateStatus.FAILED
                blockers.append(f"{gate} result is not ready")
        gates["dynamic_real_llm_candidates"] = GateStatus.IMPLEMENTED_NOT_RUN
        blockers.append("Dynamic real-LLM DRAFT candidates are not yet connected to official evidence.")
        return FormalResearchAcceptanceVerdict(
            verdict_id="formal_research_acceptance.current",
            gates=gates,
            evidence_hashes=hashes,
            blockers=tuple(blockers),
        )


def _read_json(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail(FailureCode.INCOMPLETE_RESULT, "acceptance result artifact is missing or invalid", error=type(exc).__name__)


def _declared_hash(payload: Mapping[str, object]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
