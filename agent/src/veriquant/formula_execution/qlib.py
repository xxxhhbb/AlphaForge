"""Certified Qlib-expression backend for the backend-neutral score boundary.

The backend never accepts an expression string.  It consumes the certified AST
and a read-only PIT provider manifest, invokes the pinned in-memory worker, and
converts only factor scores into the common ``FactorScoreArtifact`` contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from ..contracts import FailureCode
from ..contracts.base import fail
from ..data import MarketSnapshot
from ..frozen_qlib_provider import FrozenQlibProviderManifest
from ..qlib_candidate_translation import (
    QlibCandidateTranslationResult,
    QlibCandidateTranslationStatus,
)
from ..qlib_expression.api import (
    QlibExpressionPolicy,
    QlibFieldRole,
    QlibOperatorTrust,
    STATIC_QLIB_OPERATOR_MANIFEST,
)
from ..qlib_operator_subprocess import (
    QlibOperatorSubprocessRunner,
    QlibSubprocessConfig,
    QlibSubprocessFailure,
    QlibSubprocessFailureCode,
)
from ..qlib_operator_worker.api import (
    QlibInMemoryFeatureRow,
    QlibOperatorExecutionRequest,
    QlibOperatorWorkerFailure,
    QlibOperatorWorkerFailureCode,
    QlibRuntimeIdentity,
    execute_certified_qlib_operators,
)
from .models import (
    FactorScoreArtifact,
    FactorScorePoint,
    FormulaExecutionRequest,
    FormulaProgramKind,
)


# Real pyqlib 0.9.7 parity evidence found missing-value or weighting behavior
# that differs from the platform's frozen semantic policy.  The low-level
# worker can execute these classes for auxiliary parity tests, but the trusted
# formula backend must not admit them to official Evidence.
KNOWN_DIFFERENCE_OPERATOR_NAMES = frozenset({"IdxMax", "IdxMin", "WMA", "If"})


@dataclass(frozen=True, slots=True)
class CertifiedQlibProgramBundle:
    """System-owned program and PIT input bundle; no authority is delegated."""

    translation: QlibCandidateTranslationResult
    provider_manifest: FrozenQlibProviderManifest

    def __post_init__(self) -> None:
        if self.translation.status is not QlibCandidateTranslationStatus.CERTIFIED:
            fail(FailureCode.INVALID_STATE, "Qlib program bundle requires a certified translation")
        if self.translation.certified_expression is None or self.translation.program_hash is None:
            fail(FailureCode.MISSING_REQUIRED_FIELD, "certified Qlib program is incomplete")
        if self.provider_manifest.requirement.required_fields != self.translation.required_fields:
            fail(FailureCode.INVALID_HASH, "provider fields do not bind certified Qlib fields")
        blocked = set(self.translation.operators) & KNOWN_DIFFERENCE_OPERATOR_NAMES
        if blocked:
            fail(
                FailureCode.INVALID_FACTOR_DAG,
                "Qlib program uses operators with known semantic differences",
                operators=tuple(sorted(blocked)),
            )


class CertifiedQlibExpressionBackend:
    """Compatibility backend that executes pinned Qlib in-process.

    The trusted default service does not register this backend. Production
    score calculation uses :class:`IsolatedCertifiedQlibExpressionBackend`.
    """

    kind = FormulaProgramKind.CERTIFIED_QLIB_EXPRESSION

    def execute(
        self,
        *,
        request: FormulaExecutionRequest,
        program: CertifiedQlibProgramBundle,
        snapshot: MarketSnapshot,
    ) -> FactorScoreArtifact:
        if not isinstance(program, CertifiedQlibProgramBundle):
            fail(FailureCode.INVALID_VALUE, "Qlib backend requires CertifiedQlibProgramBundle")
        translation = program.translation
        certified = translation.certified_expression
        assert certified is not None and translation.program_hash is not None
        if translation.program_hash != request.program.program_hash:
            fail(FailureCode.INVALID_HASH, "Qlib program hash does not match execution request")
        if translation.policy_hash != request.program.policy_hash:
            fail(FailureCode.INVALID_HASH, "Qlib policy hash does not match execution request")
        expected_runtime = QlibRuntimeIdentity.expected().runtime_hash
        if request.program.runtime_hash != expected_runtime:
            fail(FailureCode.INVALID_HASH, "Qlib runtime hash is not the pinned runtime identity")
        if translation.required_fields != request.program.required_fields:
            fail(FailureCode.INVALID_HASH, "Qlib required fields do not match execution request")

        active_keys = _validate_manifest(program.provider_manifest, snapshot)
        sessions = program.provider_manifest.calendar.sessions
        symbols = tuple(
            sorted(
                {
                    symbol
                    for members in program.provider_manifest.universe.members_by_session.values()
                    for symbol in members
                }
            )
        )
        by_key = {
            (row.session, row.symbol): row
            for row in program.provider_manifest.features.rows
        }
        rows = tuple(
            QlibInMemoryFeatureRow(
                event_time=session,
                symbol=symbol,
                values=(
                    {field: None for field in translation.required_fields}
                    if (session, symbol) not in by_key
                    else dict(by_key[(session, symbol)].values)
                ),
            )
            for session in sessions
            for symbol in symbols
        )
        worker_request = QlibOperatorExecutionRequest(
            program=certified,
            snapshot_hash=request.snapshot_hash,
            universe_hash=request.universe_hash,
            output_symbols=symbols,
            rows=rows,
        )
        worker_result = self._execute_worker(worker_request, translation)
        if isinstance(worker_result, QlibOperatorWorkerFailure):
            _raise_worker_failure(worker_result)
        if isinstance(worker_result, QlibSubprocessFailure):
            _raise_subprocess_failure(worker_result)
        if worker_result.program_hash != certified.expression_hash:
            fail(FailureCode.INVALID_HASH, "worker output does not bind certified expression")
        if worker_result.runtime_hash != expected_runtime:
            fail(FailureCode.INVALID_HASH, "worker output does not bind pinned runtime")
        values = tuple(
            FactorScorePoint(
                event_time=row.event_time,
                symbol=row.symbol,
                score=row.score,
            )
            for row in worker_result.rows
            if (row.event_time, row.symbol) in active_keys
        )
        if len(values) != len(active_keys):
            fail(FailureCode.INCOMPLETE_RESULT, "Qlib scores do not cover the historical universe")
        return FactorScoreArtifact(
            program_hash=request.program.program_hash,
            snapshot_hash=request.snapshot_hash,
            universe_hash=request.universe_hash,
            runtime_hash=request.program.runtime_hash,
            policy_hash=request.program.policy_hash,
            seed=request.seed,
            values=values,
        )

    def _execute_worker(self, worker_request, translation):
        return execute_certified_qlib_operators(worker_request)


class IsolatedCertifiedQlibExpressionBackend(CertifiedQlibExpressionBackend):
    """Execute one certified request per fixed, bounded, no-network process."""

    def __init__(self, runner: QlibOperatorSubprocessRunner) -> None:
        self._runner = runner

    def _execute_worker(self, worker_request, translation):
        policy = _reconstruct_translation_policy(translation)
        if policy.policy_hash != translation.policy_hash:
            fail(FailureCode.INVALID_HASH, "Qlib translation policy cannot be reconstructed")
        return self._runner.execute(worker_request, policy=policy)


def configured_isolated_qlib_backend() -> IsolatedCertifiedQlibExpressionBackend | None:
    """Build the trusted backend only from operator-owned local configuration."""

    raw = os.environ.get("VERIQUANT_QLIB_PYTHON", "").strip()
    if not raw:
        # The API already treats agent/.env as its operator-owned local
        # configuration file.  Read only this non-secret runtime selector;
        # never load or export the full dotenv mapping.
        dotenv = Path(__file__).resolve().parents[3] / ".env"
        if dotenv.is_file():
            try:
                for line in dotenv.read_text(encoding="utf-8").splitlines():
                    key, separator, value = line.partition("=")
                    if separator and key.strip() == "VERIQUANT_QLIB_PYTHON":
                        raw = value.strip().strip('"').strip("'")
                        break
            except OSError:
                return None
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        return None
    repository_root = Path(__file__).resolve().parents[3]
    sandbox_root = repository_root / ".autobuild" / "veriquant" / "qlib-worker"
    try:
        config = QlibSubprocessConfig.pin(
            python_path=path,
            sandbox_root=sandbox_root,
            timeout_seconds=120.0,
        )
    except (OSError, ValueError):
        return None
    return IsolatedCertifiedQlibExpressionBackend(QlibOperatorSubprocessRunner(config))


def _reconstruct_translation_policy(
    translation: QlibCandidateTranslationResult,
) -> QlibExpressionPolicy:
    trusted_operators = tuple(
        item.name
        for item in STATIC_QLIB_OPERATOR_MANIFEST.operators
        if item.trust is not QlibOperatorTrust.EXCLUDED
    )
    return QlibExpressionPolicy(
        field_roles={
            field: QlibFieldRole.FEATURE
            for field in translation.confirmed_field_whitelist
        },
        allowed_operators=trusted_operators,
        max_window=2_520,
        max_ref_periods=2_520,
    )


def _validate_manifest(
    manifest: FrozenQlibProviderManifest,
    snapshot: MarketSnapshot,
) -> frozenset[tuple[object, str]]:
    if manifest.dataset_id != snapshot.dataset_id:
        fail(FailureCode.INVALID_HASH, "Qlib provider dataset does not match frozen snapshot")
    if manifest.availability_proof.decision_time != snapshot.decision_time:
        fail(FailureCode.INVALID_HASH, "Qlib PIT proof decision_time does not match snapshot")
    snapshot_rows = {(row.event_time, row.symbol): row for row in snapshot.rows}
    provider_rows = {(row.session, row.symbol): row for row in manifest.features.rows}
    if set(snapshot_rows) != set(provider_rows):
        fail(FailureCode.INVALID_HASH, "Qlib provider rows do not match historical snapshot membership")
    for key, provider_row in provider_rows.items():
        source = snapshot_rows[key]
        expected = {field: source.values[field] for field in manifest.features.fields}
        if dict(provider_row.values) != expected:
            fail(FailureCode.INVALID_HASH, "Qlib provider feature values do not match frozen snapshot")
    return frozenset(provider_rows)


def _raise_worker_failure(failure: QlibOperatorWorkerFailure) -> None:
    if failure.code in {
        QlibOperatorWorkerFailureCode.QLIB_RUNTIME_UNAVAILABLE,
        QlibOperatorWorkerFailureCode.QLIB_RUNTIME_VERSION_MISMATCH,
        QlibOperatorWorkerFailureCode.QLIB_RUNTIME_SOURCE_MISMATCH,
    }:
        fail(FailureCode.ADAPTER_UNAVAILABLE, failure.message)
    if failure.code in {
        QlibOperatorWorkerFailureCode.UNTRUSTED_PROGRAM,
        QlibOperatorWorkerFailureCode.PROGRAM_IDENTITY_MISMATCH,
        QlibOperatorWorkerFailureCode.MANIFEST_MISMATCH,
        QlibOperatorWorkerFailureCode.EXCLUDED_OPERATOR,
        QlibOperatorWorkerFailureCode.UNSUPPORTED_NODE,
    }:
        fail(FailureCode.INVALID_FACTOR_DAG, failure.message)
    fail(FailureCode.INCOMPLETE_RESULT, failure.message)


def _raise_subprocess_failure(failure: QlibSubprocessFailure) -> None:
    if failure.code in {
        QlibSubprocessFailureCode.PYTHON_NOT_PINNED,
        QlibSubprocessFailureCode.WORKER_IDENTITY_MISMATCH,
        QlibSubprocessFailureCode.LAUNCH_FAILED,
    }:
        fail(FailureCode.ADAPTER_UNAVAILABLE, failure.message)
    if failure.code in {
        QlibSubprocessFailureCode.INVALID_REQUEST,
        QlibSubprocessFailureCode.OUTPUT_INVALID,
    }:
        fail(FailureCode.INVALID_FACTOR_DAG, failure.message)
    fail(FailureCode.INCOMPLETE_RESULT, failure.message)


__all__ = [
    "CertifiedQlibExpressionBackend",
    "CertifiedQlibProgramBundle",
    "IsolatedCertifiedQlibExpressionBackend",
    "KNOWN_DIFFERENCE_OPERATOR_NAMES",
    "configured_isolated_qlib_backend",
]
