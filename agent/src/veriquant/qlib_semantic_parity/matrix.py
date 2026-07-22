"""Deterministic matrix comparison helpers without a Qlib dependency."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .models import OperatorParityEvidence, SemanticParityStatus, stable_hash
from .policy import DIFFERENCE_CODES, EXPECTED_DISPOSITIONS, OPERATOR_POLICIES


@dataclass(frozen=True, slots=True)
class ParityObservation:
    case_id: str
    reference: tuple[float | None, ...]
    pyqlib: tuple[float | None, ...]

    def __post_init__(self) -> None:
        if not self.case_id or len(self.reference) != len(self.pyqlib) or not self.reference:
            raise ValueError("invalid parity observation")

    @property
    def case_hash(self) -> str:
        return stable_hash({"case_id": self.case_id, "reference": self.reference, "pyqlib": self.pyqlib})

    def matches(self, *, absolute_tolerance: float = 1e-10) -> bool:
        for expected, actual in zip(self.reference, self.pyqlib, strict=True):
            if expected is None or actual is None:
                if expected is not actual:
                    return False
            elif not math.isclose(expected, actual, rel_tol=1e-10, abs_tol=absolute_tolerance):
                return False
        return True


def build_operator_evidence(operator: str, observations: Sequence[ParityObservation]) -> OperatorParityEvidence:
    if operator not in OPERATOR_POLICIES or not observations:
        raise ValueError("unknown operator or empty observations")
    if len({item.case_id for item in observations}) != len(observations):
        raise ValueError("parity case identifiers must be unique per operator")
    matches = tuple(item.matches() for item in observations)
    mismatches = matches.count(False)
    status = EXPECTED_DISPOSITIONS[operator]
    if status is SemanticParityStatus.CERTIFIED_EQUIVALENT and mismatches:
        raise ValueError(f"{operator} violated certified reference semantics")
    if status is SemanticParityStatus.KNOWN_DIFFERENCE and not mismatches:
        raise ValueError(f"{operator} expected difference was not observed")
    return OperatorParityEvidence(
        operator=operator,
        status=status,
        policy_hash=OPERATOR_POLICIES[operator].policy_hash,
        cases_total=len(observations),
        matched_cases=matches.count(True),
        mismatched_cases=mismatches,
        case_hashes=tuple(item.case_hash for item in observations),
        difference_codes=DIFFERENCE_CODES.get(operator, ()),
    )
