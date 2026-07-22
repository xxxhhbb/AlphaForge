"""Fail-closed dispatch for formula execution backends."""

from __future__ import annotations

from typing import Any, Protocol

from ..contracts import FailureCode
from ..contracts.base import fail
from ..data import MarketSnapshot, canonical_content_hash
from .models import (
    FactorScoreArtifact,
    FormulaExecutionRequest,
    FormulaProgramKind,
)


class FormulaExecutionBackend(Protocol):
    kind: FormulaProgramKind

    def execute(
        self,
        *,
        request: FormulaExecutionRequest,
        program: Any,
        snapshot: MarketSnapshot,
    ) -> FactorScoreArtifact: ...


class FormulaExecutionService:
    """Dispatch only to an explicitly registered backend and verify its output."""

    def __init__(self, *backends: FormulaExecutionBackend) -> None:
        self._backends: dict[FormulaProgramKind, FormulaExecutionBackend] = {}
        for backend in backends:
            if backend.kind in self._backends:
                fail(FailureCode.INVALID_VALUE, "duplicate formula execution backend")
            self._backends[backend.kind] = backend

    def execute(
        self,
        *,
        request: FormulaExecutionRequest,
        program: Any,
        snapshot: MarketSnapshot,
    ) -> FactorScoreArtifact:
        if not isinstance(request, FormulaExecutionRequest) or not isinstance(snapshot, MarketSnapshot):
            fail(FailureCode.INVALID_VALUE, "formula execution requires typed request and snapshot")
        if snapshot.artifact_hash != request.snapshot_hash:
            fail(FailureCode.INVALID_HASH, "snapshot does not match formula execution request")
        universe_hash = canonical_content_hash(snapshot.historical_universe)
        if universe_hash != request.universe_hash:
            fail(FailureCode.INVALID_HASH, "historical universe does not match formula execution request")
        missing = set(request.program.required_fields) - set(snapshot.fields)
        if missing:
            fail(FailureCode.UNKNOWN_FIELD, "snapshot lacks required formula fields", fields=tuple(sorted(missing)))
        backend = self._backends.get(request.program.kind)
        if backend is None:
            fail(FailureCode.ADAPTER_UNAVAILABLE, "formula execution backend is unavailable")
        artifact = backend.execute(request=request, program=program, snapshot=snapshot)
        expected = {
            "program_hash": request.program.program_hash,
            "snapshot_hash": request.snapshot_hash,
            "universe_hash": request.universe_hash,
            "runtime_hash": request.program.runtime_hash,
            "policy_hash": request.program.policy_hash,
            "seed": request.seed,
        }
        for field, value in expected.items():
            if getattr(artifact, field) != value:
                fail(FailureCode.INVALID_HASH, f"formula backend output does not bind {field}")
        return artifact
