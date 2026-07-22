"""Compatibility import for the scale qualification planner.

The earlier C8-only readiness draft has been replaced by the bounded manifest
planner.  No C8 artifact is promoted to production scale by this module.
"""

from .models import ScaleQualificationOutcome, ScaleQualificationVerdict
from .planner import RealAshareScalePlanner

__all__ = [
    "RealAshareScalePlanner",
    "ScaleQualificationOutcome",
    "ScaleQualificationVerdict",
]
