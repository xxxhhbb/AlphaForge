"""Narrow machine-readable API for Qlib migration coverage."""

from __future__ import annotations

from pathlib import Path

from .models import MigrationDataPolicy, QlibMigrationCoverageReport
from .scanner import build_qlib_migration_coverage


def qlib_migration_coverage_manifest(
    zoo_root: Path | str | None = None,
    *,
    data_policy: MigrationDataPolicy | None = None,
) -> dict[str, object]:
    """Return the complete stable record manifest."""

    return build_qlib_migration_coverage(zoo_root, data_policy=data_policy).to_dict()


def qlib_migration_coverage_summary(
    zoo_root: Path | str | None = None,
    *,
    data_policy: MigrationDataPolicy | None = None,
) -> dict[str, object]:
    """Return counts, identities, eligibility dimensions, and operator backlog."""

    return build_qlib_migration_coverage(zoo_root, data_policy=data_policy).summary()


__all__ = [
    "MigrationDataPolicy",
    "QlibMigrationCoverageReport",
    "qlib_migration_coverage_manifest",
    "qlib_migration_coverage_summary",
]
