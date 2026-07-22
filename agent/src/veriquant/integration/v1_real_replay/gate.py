"""Fail-closed adapter from real visible Evidence to real shadow replay readiness."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from veriquant.contracts import FailureCode
from veriquant.contracts.base import fail
from veriquant.integration.v1_real_official.models import RealOfficialEvidenceRun
from veriquant.real_vintage_replay.api import RealVintageReplayController
from veriquant.shadow_replay.api import ReplayStatus, ShadowReplayPolicy

from .models import PromotionChallengerReceipt, RealReplayGateResult, RealReplayGateStatus
from .protocols import IndependentPromotionReceiptProvider


PINNED_OFFICIAL_RUN_HASH = "sha256:45ce4eefbd4a728854121f95a4564d4613720ef09f68aa60a89af350f192d2ca"
PINNED_OFFICIAL_RELATIVE_PATH = Path(
    ".autobuild/veriquant/parallel/ui_agent_c/real_official_run/official_evidence_run.json"
)


class RealReplayReadinessGate:
    """Validate C's output and require a separate Promotion receipt.

    This class does not create a PromotionDecision or a CHALLENGER.  It only
    verifies an externally supplied minimal receipt and delegates replay to the
    existing deterministic replay controller.
    """

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    def load_official_run(self) -> RealOfficialEvidenceRun:
        path = (self.repo_root / PINNED_OFFICIAL_RELATIVE_PATH).resolve()
        if self.repo_root not in path.parents:
            fail(FailureCode.UNAUTHORIZED_CAPABILITY, "official evidence path escaped repository")
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(FailureCode.LEDGER_CORRUPTION, f"official evidence artifact is unreadable: {type(exc).__name__}")
        declared_hash = payload.get("artifact_hash")
        contract_payload = dict(payload)
        contract_payload.pop("artifact_hash", None)
        run = RealOfficialEvidenceRun.from_payload(contract_payload)
        if run.artifact_hash != PINNED_OFFICIAL_RUN_HASH:
            fail(FailureCode.INVALID_HASH, "real official Evidence run hash changed")
        if declared_hash != run.artifact_hash:
            fail(FailureCode.INVALID_HASH, "declared real official Evidence hash changed")
        if not run.official_evidence_run or run.promotion_run or run.challenger_created:
            fail(FailureCode.INVALID_STATE, "Agent C authority flags are invalid")
        if run.official_tushare_source:
            fail(FailureCode.AUXILIARY_MASQUERADE, "approved proxy claimed official Tushare")
        return run

    def inspect(self, provider: IndependentPromotionReceiptProvider) -> tuple[RealReplayGateResult, PromotionChallengerReceipt | None]:
        run = self.load_official_run()
        receipt = provider.challenger_for_official_run(run.artifact_hash)
        if receipt is None:
            return (
                RealReplayGateResult(
                    status=RealReplayGateStatus.BLOCKED,
                    official_run_hash=run.artifact_hash,
                    official_trial_ledger_tip_hash=run.trial_ledger_tip_hash,
                    promotion_receipt_hash=None,
                    replay_manifest_hash=None,
                    journal_tip_hash=None,
                    sessions_committed=0,
                    fixture_only=False,
                    real_challenger_used=False,
                    failure_code=FailureCode.UPSTREAM_BLOCKED,
                    failure_message="real visible Evidence exists, but no independent real-data CHALLENGER receipt exists",
                ),
                None,
            )
        self._verify_receipt(run, receipt)
        return (
            RealReplayGateResult(
                status=RealReplayGateStatus.READY,
                official_run_hash=run.artifact_hash,
                official_trial_ledger_tip_hash=run.trial_ledger_tip_hash,
                promotion_receipt_hash=receipt.artifact_hash,
                replay_manifest_hash=None,
                journal_tip_hash=None,
                sessions_committed=0,
                fixture_only=False,
                real_challenger_used=True,
            ),
            receipt,
        )

    def run(
        self,
        *,
        provider: IndependentPromotionReceiptProvider,
        controller: RealVintageReplayController,
        policy: ShadowReplayPolicy,
    ) -> RealReplayGateResult:
        readiness, _ = self.inspect(provider)
        if readiness.status is RealReplayGateStatus.BLOCKED:
            return readiness
        report = controller.run(policy)
        if report.manifest.status is not ReplayStatus.READY or len(report.daily_records) != 20:
            last = report.daily_records[-1]
            return RealReplayGateResult(
                status=RealReplayGateStatus.BLOCKED,
                official_run_hash=readiness.official_run_hash,
                official_trial_ledger_tip_hash=readiness.official_trial_ledger_tip_hash,
                promotion_receipt_hash=readiness.promotion_receipt_hash,
                replay_manifest_hash=None,
                journal_tip_hash=None,
                sessions_committed=0,
                fixture_only=False,
                real_challenger_used=True,
                failure_code=FailureCode(last.failure_code or FailureCode.EXECUTION_HALTED.value),
                failure_message=last.failure_message or "real shadow replay halted",
            )
        return RealReplayGateResult(
            status=RealReplayGateStatus.COMPLETED,
            official_run_hash=readiness.official_run_hash,
            official_trial_ledger_tip_hash=readiness.official_trial_ledger_tip_hash,
            promotion_receipt_hash=readiness.promotion_receipt_hash,
            replay_manifest_hash=report.manifest.artifact_hash,
            journal_tip_hash=report.manifest.journal_tip_hash,
            sessions_committed=20,
            fixture_only=False,
            real_challenger_used=True,
        )

    @staticmethod
    def _verify_receipt(run: RealOfficialEvidenceRun, receipt: PromotionChallengerReceipt) -> None:
        if receipt.official_run_hash != run.artifact_hash:
            fail(FailureCode.INVALID_HASH, "Promotion receipt refers to another official run")
        factor = next(
            (item for item in run.factors if item.identity_hash == receipt.official_factor_identity_hash),
            None,
        )
        if factor is None or factor.validation_evidence is None or factor.factor_dag_hash is None:
            fail(FailureCode.INVALID_ALPHA_SOURCE, "Promotion receipt does not bind a successful official factor")
        if receipt.evidence_hash != factor.validation_evidence.artifact_hash:
            fail(FailureCode.INVALID_HASH, "Promotion receipt evidence hash does not bind validation Evidence")
        if receipt.factor_hash != factor.factor_dag_hash:
            fail(FailureCode.INVALID_HASH, "Promotion receipt factor hash does not bind FactorDAG")
        if receipt.available_time < run.decision_time:
            fail(FailureCode.INVALID_TIME_ORDER, "Promotion receipt predates official evaluation")


class NoPromotionReceiptProvider:
    """Production-safe default while independent real-data Promotion is absent."""

    def challenger_for_official_run(self, official_run_hash: str) -> None:
        return None


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()
