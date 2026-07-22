"""Minimal interoperability protocols for future certified-expression packages."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    FormulaExecutionRequest,
    QlibRuntimeManifest,
    RuntimeFeatureBatch,
    RuntimeResult,
)


@runtime_checkable
class CertifiedExpressionIdentity(Protocol):
    """Identity-only view required from a certified expression compiler."""

    @property
    def artifact_hash(self) -> str: ...

    @property
    def operator_manifest_hash(self) -> str: ...

    @property
    def required_fields(self) -> tuple[str, ...]: ...


@runtime_checkable
class FrozenFeatureProvider(Protocol):
    """Read-only PIT provider surface; labels are deliberately absent."""

    def load_features(self, request: FormulaExecutionRequest) -> RuntimeFeatureBatch: ...


@runtime_checkable
class FormulaExecutionBackend(Protocol):
    """Unified backend contract for formula execution."""

    @property
    def manifest(self) -> QlibRuntimeManifest: ...

    def execute(
        self,
        *,
        request: FormulaExecutionRequest,
        program: CertifiedExpressionIdentity,
        provider: FrozenFeatureProvider,
    ) -> RuntimeResult: ...

