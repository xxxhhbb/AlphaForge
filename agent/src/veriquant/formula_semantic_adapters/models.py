"""Closed contracts for the built-in formula semantic adapters."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


ADAPTER_SCHEMA_VERSION = "veriquant.formula-semantic-adapter/v1"
ADAPTER_CATALOG_VERSION = "veriquant.safe-formula-adapters/2026-07-21-v1"


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AdapterFailureCode(str, Enum):
    UNKNOWN_ADAPTER = "UNKNOWN_ADAPTER"
    INVALID_WINDOW = "INVALID_WINDOW"
    INVALID_INPUT = "INVALID_INPUT"
    SHAPE_MISMATCH = "SHAPE_MISMATCH"


class SemanticAdapterError(ValueError):
    def __init__(self, code: AdapterFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SemanticAdapterPolicy:
    name: str
    arity: int
    windowed: bool
    semantics: Mapping[str, object]
    schema_version: str = ADAPTER_SCHEMA_VERSION
    catalog_version: str = ADAPTER_CATALOG_VERSION

    def __post_init__(self) -> None:
        if not self.name.startswith("Safe") or self.arity < 1:
            raise ValueError("invalid built-in adapter policy")
        if self.schema_version != ADAPTER_SCHEMA_VERSION or self.catalog_version != ADAPTER_CATALOG_VERSION:
            raise ValueError("unsupported adapter policy identity")
        object.__setattr__(self, "semantics", MappingProxyType(dict(sorted(self.semantics.items()))))

    def to_canonical(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "name": self.name,
            "arity": self.arity,
            "windowed": self.windowed,
            "semantics": dict(self.semantics),
        }

    @property
    def policy_hash(self) -> str:
        return canonical_hash(self.to_canonical())

    @property
    def adapter_hash(self) -> str:
        return canonical_hash(
            {
                "kind": "SYSTEM_BUILTIN_CLOSED_ADAPTER",
                "implementation_version": self.catalog_version,
                "policy_hash": self.policy_hash,
            }
        )


@dataclass(frozen=True, slots=True)
class SemanticAdapterResult:
    adapter_name: str
    values: tuple[float | None, ...]
    policy_hash: str
    adapter_hash: str
    request_hash: str

    def __post_init__(self) -> None:
        for value in (self.policy_hash, self.adapter_hash, self.request_hash):
            if not value.startswith("sha256:") or len(value) != 71:
                raise ValueError("invalid adapter result identity")

    def to_canonical(self) -> dict[str, object]:
        return {
            "adapter_name": self.adapter_name,
            "values": list(self.values),
            "policy_hash": self.policy_hash,
            "adapter_hash": self.adapter_hash,
            "request_hash": self.request_hash,
        }

    @property
    def result_hash(self) -> str:
        return canonical_hash(self.to_canonical())


__all__ = [
    "ADAPTER_CATALOG_VERSION",
    "ADAPTER_SCHEMA_VERSION",
    "AdapterFailureCode",
    "SemanticAdapterError",
    "SemanticAdapterPolicy",
    "SemanticAdapterResult",
    "canonical_hash",
]
