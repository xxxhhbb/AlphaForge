"""Daily paper-journal reconciliation with fail-closed HALT semantics."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Mapping

from ..contracts import FailureCode
from ..contracts.base import fail, require_aware_datetime, require_finite_number
from ..data import canonical_content_hash
from .execution import PaperExecutionEngine
from .journal import PaperJournal
from .models import ReconciliationReport, ReconciliationStatus


class DailyReconciler:
    reconciler_id = "paper-daily-reconciler-v1"

    def reconcile(
        self,
        *,
        journal: PaperJournal,
        marks: Mapping[str, Decimal],
        reconciled_at: datetime,
        tolerance: Decimal = Decimal("0.0001"),
        external_cash: Decimal | None = None,
        external_positions: Mapping[str, Decimal] | None = None,
        execution_engine: PaperExecutionEngine | None = None,
    ) -> ReconciliationReport:
        if not isinstance(journal, PaperJournal):
            fail(FailureCode.INVALID_VALUE, "journal must be PaperJournal")
        require_aware_datetime(reconciled_at, "reconciled_at")
        require_finite_number(tolerance, "tolerance")
        if tolerance < 0:
            fail(FailureCode.INVALID_VALUE, "tolerance cannot be negative")
        initial_state = journal.replay()
        breaches: list[str] = []
        if initial_state.halted:
            breaches.append("JOURNAL_ALREADY_HALTED")
        if execution_engine is not None and execution_engine.halted:
            breaches.append("AMBIGUOUS_EXECUTION_STATE")
        if breaches:
            source_hash = canonical_content_hash(
                {"breaches": breaches, "reconciled_at": reconciled_at}
            )
            state = journal.append_halt(
                reason=";".join(breaches),
                event_time=reconciled_at,
                source_hash=source_hash,
            )
            return self._report(
                state=state,
                status=ReconciliationStatus.HALT,
                breaches=tuple(breaches),
                reconciled_at=reconciled_at,
            )
        try:
            state = journal.mark_to_market(marks=marks, event_time=reconciled_at)
        except Exception as exc:
            source_hash = canonical_content_hash(
                {"reason": "MARK_TO_MARKET_FAILED", "reconciled_at": reconciled_at}
            )
            state = journal.append_halt(
                reason="MARK_TO_MARKET_FAILED",
                event_time=reconciled_at,
                source_hash=source_hash,
            )
            if isinstance(exc, KeyboardInterrupt):
                raise
            return self._report(
                state=state,
                status=ReconciliationStatus.HALT,
                breaches=("MARK_TO_MARKET_FAILED",),
                reconciled_at=reconciled_at,
            )
        difference = state.equity - state.expected_equity
        if abs(difference) > tolerance:
            breaches.append("ACCOUNTING_IDENTITY_MISMATCH")
        if external_cash is not None:
            require_finite_number(external_cash, "external_cash")
            if abs(state.cash - external_cash) > tolerance:
                breaches.append("CASH_MISMATCH")
        if external_positions is not None:
            normalized = {symbol: Decimal(value) for symbol, value in external_positions.items()}
            symbols = set(state.positions) | set(normalized)
            if any(
                abs(state.positions.get(symbol, Decimal("0")) - normalized.get(symbol, Decimal("0")))
                > tolerance
                for symbol in symbols
            ):
                breaches.append("POSITION_MISMATCH")
        if breaches:
            source_hash = canonical_content_hash(
                {
                    "state_hash": state.artifact_hash,
                    "breaches": breaches,
                    "reconciled_at": reconciled_at,
                }
            )
            state = journal.append_halt(
                reason=";".join(breaches),
                event_time=reconciled_at,
                source_hash=source_hash,
            )
            return self._report(
                state=state,
                status=ReconciliationStatus.HALT,
                breaches=tuple(breaches),
                reconciled_at=reconciled_at,
            )
        return self._report(
            state=state,
            status=ReconciliationStatus.RECONCILED,
            breaches=(),
            reconciled_at=reconciled_at,
        )

    def _report(
        self,
        *,
        state,
        status: ReconciliationStatus,
        breaches: tuple[str, ...],
        reconciled_at: datetime,
    ) -> ReconciliationReport:
        difference = state.equity - state.expected_equity
        report_hash = canonical_content_hash(
            {
                "reconciler_id": self.reconciler_id,
                "journal_hash": state.artifact_hash,
                "status": status.value,
                "difference": difference,
                "breaches": breaches,
                "reconciled_at": reconciled_at,
            }
        )
        return ReconciliationReport(
            report_id="reconcile-" + report_hash.removeprefix("sha256:")[:24],
            journal_hash=state.artifact_hash,
            status=status,
            actual_equity=state.equity,
            expected_equity=state.expected_equity,
            difference=difference,
            breaches=breaches,
            reconciled_at=reconciled_at,
        )
