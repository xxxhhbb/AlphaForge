"""Deterministic data planning for AlphaForge research questions.

This package plans data requirements only. It does not fetch data, compile
FactorDAGs, run PIT snapshots, calculate official Evidence, or promote results.
"""

from .service import (
    DataAvailabilitySnapshot,
    DataPlanningResult,
    DeterministicDataPlanner,
    FieldAvailability,
    PlannedFieldRole,
)

__all__ = [
    "DataAvailabilitySnapshot",
    "DataPlanningResult",
    "DeterministicDataPlanner",
    "FieldAvailability",
    "PlannedFieldRole",
]
