"""Immutable ID and cursor-paginated access to the Qlib qualification view."""

from __future__ import annotations

from .models import (
    ProjectionError,
    ProjectionFailureCode,
    QlibExecutionQualification,
    QlibFactorLibraryProjection,
    QlibFactorProjectionPage,
    QlibFactorProjectionRecord,
)


class QlibFactorLibraryProjectionRepository:
    def __init__(self, projection: QlibFactorLibraryProjection) -> None:
        if not isinstance(projection, QlibFactorLibraryProjection):
            raise ProjectionError(ProjectionFailureCode.INVALID_QUERY, "projection is required")
        self._projection = projection
        self._by_id = {record.factor_id: record for record in projection.records}

    @property
    def projection_hash(self) -> str:
        return self._projection.projection_hash

    def get_by_id(self, factor_id: str) -> QlibFactorProjectionRecord:
        try:
            return self._by_id[factor_id]
        except (KeyError, TypeError) as exc:
            raise ProjectionError(
                ProjectionFailureCode.FACTOR_NOT_FOUND,
                "factor id is not present in the frozen projection",
                factor_id=factor_id,
            ) from exc

    def page(
        self,
        *,
        page_size: int = 50,
        cursor: str | None = None,
        qualification: QlibExecutionQualification | str | None = None,
    ) -> QlibFactorProjectionPage:
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ProjectionError(ProjectionFailureCode.INVALID_QUERY, "page_size must be an integer from 1 to 100")
        if qualification is not None and not isinstance(qualification, QlibExecutionQualification):
            try:
                qualification = QlibExecutionQualification(qualification)
            except (TypeError, ValueError) as exc:
                raise ProjectionError(ProjectionFailureCode.INVALID_QUERY, "unknown qualification filter") from exc
        records = tuple(
            record
            for record in self._projection.records
            if qualification is None or record.execution_qualification is qualification
        )
        start = 0
        if cursor is not None:
            prefix = self.projection_hash.removeprefix("sha256:") + ":"
            if not isinstance(cursor, str) or not cursor.startswith(prefix):
                raise ProjectionError(ProjectionFailureCode.STALE_CURSOR, "cursor does not belong to this projection")
            factor_id = cursor[len(prefix) :]
            positions = {record.factor_id: index for index, record in enumerate(records)}
            if factor_id not in positions:
                raise ProjectionError(ProjectionFailureCode.STALE_CURSOR, "cursor is invalid for this filtered view")
            start = positions[factor_id] + 1
        selected = records[start : start + page_size]
        next_cursor = None
        if start + len(selected) < len(records) and selected:
            next_cursor = self.projection_hash.removeprefix("sha256:") + ":" + selected[-1].factor_id
        return QlibFactorProjectionPage(
            records=selected,
            total_matching=len(records),
            next_cursor=next_cursor,
            projection_hash=self.projection_hash,
        )


__all__ = ["QlibFactorLibraryProjectionRepository"]
