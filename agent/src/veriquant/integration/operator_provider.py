"""Fixed, key-free providers for the standalone VeriQuant operator app.

The provider delegates financial computation to the existing deterministic
adapter and returns only its content hash to the orchestration plane.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from veriquant.integration.v1_real_official import RealOfficialEvidenceAdapter, RealOfficialEvidenceRun
from src.veriquant.operator.models import ProviderProfile, StageDisposition, StageResult, canonical_hash
from src.veriquant.operator.registry import OperatorRegistry
from src.veriquant.integration.workbench import publish_workbench_run


REAL_PROFILE_ID = "approved-proxy-visible-evidence-v1"
REAL_PROVIDER_ID = "approved-proxy-visible-evidence"
REAL_STAGE_ID = "factor_dag_pit_official_evidence"
CONTROL_PROFILE_ID = "control-smoke-v1"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", delete=False, dir=str(path.parent), suffix=".tmp"
    )
    temporary = Path(handle.name)
    try:
        json.dump(dict(payload), handle, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    finally:
        if not handle.closed:
            handle.close()
        if temporary.exists():
            temporary.unlink()


class DeterministicControlProvider:
    """Inert control-plane fixture; never evidence or promotion."""

    def execute_stage(
        self,
        *,
        job_id: str,
        stage_id: str,
        seed: int,
        budget: int,
        committed_artifacts: Mapping[str, str],
    ) -> StageResult:
        return StageResult(
            disposition=StageDisposition.COMMITTED,
            artifact_hash=canonical_hash(
                {
                    "provider": "deterministic-control-fixture/v1",
                    "job_id": job_id,
                    "stage_id": stage_id,
                    "seed": seed,
                    "budget": budget,
                    "committed": dict(sorted(committed_artifacts.items())),
                }
            ),
        )


class ApprovedProxyVisibleEvidenceProvider:
    """Runs only the pinned, offline, visible Official Evidence adapter."""

    def __init__(self, *, root: str | Path, workbench_root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.workbench_root = Path(workbench_root).resolve()

    def _publish(self, *, job_id: str, result: RealOfficialEvidenceRun) -> None:
        factor_rows = []
        evidence_rows = []
        for factor in result.factors:
            factor_rows.append(
                {
                    "candidate_id": factor.factor_id,
                    "family": "approved-proxy-static-v1",
                    "state": "INCOMPLETE",
                    "operator": "restricted FactorDAG",
                    "factor_dag_hash": factor.factor_dag_hash,
                    "identity_hash": factor.identity_hash,
                }
            )
            evidence_rows.append(
                {
                    "factor_id": factor.factor_id,
                    "status": factor.status.value,
                    "train": factor.train_evidence.to_dict() if factor.train_evidence else None,
                    "validation": factor.validation_evidence.to_dict() if factor.validation_evidence else None,
                    "failure_code": factor.failure_code.value if factor.failure_code else None,
                }
            )
        publish_workbench_run(
            root=self.workbench_root,
            run_id=job_id,
            status="INCOMPLETE",
            updated_at=result.decision_time.isoformat(),
            artifacts={
                "overview": {
                    "run_id": job_id,
                    "title": "Approved proxy visible Official Evidence",
                    "status": "INCOMPLETE",
                    "decision_time": result.decision_time.isoformat(),
                    "authority": "deterministic FactorDAG/PIT/Evidence Service",
                    "provider_class": result.provider_class,
                    "source_class": result.source_class,
                    "official_tushare_source": False,
                    "research_dag": {"real_visible_evidence": "INCOMPLETE"},
                },
                "lineage": {"candidates": factor_rows},
                "evidence": {
                    "evidence_class": "OFFICIAL_EVIDENCE",
                    "sealed_redacted": True,
                    "run_artifact_hash": result.artifact_hash,
                    "trial_ledger_tip_hash": result.trial_ledger_tip_hash,
                    "factors": evidence_rows,
                },
                "critics": {"status": "NOT_RUN", "reason": "independent real-data critics have not run"},
                "promotion": {
                    "decision": {"verdict": "BLOCKED", "failure_code": "REAL_PROMOTION_NOT_RUN"},
                    "sealed_redacted": True,
                },
                "citations": {},
                "shadow_portfolio": {"status": "BLOCKED", "failure_code": "REAL_CHALLENGER_UNAVAILABLE"},
                "nav_attribution": {},
                "drift_alerts": {
                    "items": [
                        {
                            "severity": "BLOCKED",
                            "code": "REAL_PROMOTION_NOT_RUN",
                            "message": "Visible Official Evidence completed; independent Promotion and real 20-session Shadow replay remain unavailable.",
                        }
                    ]
                },
            },
        )

    def execute_stage(
        self,
        *,
        job_id: str,
        stage_id: str,
        seed: int,
        budget: int,
        committed_artifacts: Mapping[str, str],
    ) -> StageResult:
        if stage_id != REAL_STAGE_ID or committed_artifacts or budget != 2:
            return StageResult(disposition=StageDisposition.FAILED)
        output = (self.root / job_id).resolve()
        try:
            output.relative_to(self.root)
        except ValueError:
            return StageResult(disposition=StageDisposition.FAILED)
        result_path = output / "official_evidence_run.json"
        if result_path.exists():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                declared_hash = payload.pop("artifact_hash")
                result = RealOfficialEvidenceRun.from_payload(payload)
                if declared_hash != result.artifact_hash or not result.official_evidence_run:
                    return StageResult(disposition=StageDisposition.FAILED)
                self._publish(job_id=job_id, result=result)
                return StageResult(disposition=StageDisposition.COMMITTED, artifact_hash=result.artifact_hash)
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                return StageResult(disposition=StageDisposition.FAILED)
        try:
            result = RealOfficialEvidenceAdapter().run(
                output_root=output,
                run_id=f"operator-{job_id}",
                seed=seed,
                family_budget=budget,
            )
            if not result.official_evidence_run:
                return StageResult(disposition=StageDisposition.FAILED)
            _atomic_json(result_path, {**result.to_dict(), "artifact_hash": result.artifact_hash})
            self._publish(job_id=job_id, result=result)
            return StageResult(disposition=StageDisposition.COMMITTED, artifact_hash=result.artifact_hash)
        except Exception:
            # The public operator surface receives only a typed provider failure;
            # underlying data/path details are intentionally not serialized.
            return StageResult(disposition=StageDisposition.FAILED)


def build_integrated_registry(*, real_run_root: str | Path, workbench_root: str | Path) -> OperatorRegistry:
    profiles = {
        CONTROL_PROFILE_ID: ProviderProfile(
            profile_id=CONTROL_PROFILE_ID,
            provider_id="deterministic-control-fixture",
            stage_ids=("validate_request", "record_control_event", "publish_control_status"),
            max_budget=3,
        ),
        REAL_PROFILE_ID: ProviderProfile(
            profile_id=REAL_PROFILE_ID,
            provider_id=REAL_PROVIDER_ID,
            stage_ids=(REAL_STAGE_ID,),
            max_budget=2,
        ),
    }
    providers = {
        "deterministic-control-fixture": DeterministicControlProvider(),
        REAL_PROVIDER_ID: ApprovedProxyVisibleEvidenceProvider(root=real_run_root, workbench_root=workbench_root),
    }
    return OperatorRegistry(profiles=profiles, providers=providers)


__all__ = [
    "ApprovedProxyVisibleEvidenceProvider",
    "CONTROL_PROFILE_ID",
    "REAL_PROFILE_ID",
    "REAL_PROVIDER_ID",
    "REAL_STAGE_ID",
    "build_integrated_registry",
]
