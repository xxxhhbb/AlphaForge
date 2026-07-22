"""Inert, canonical contracts for FactorIR v2.

FactorIR is a description of a formula.  This module deliberately contains no
interpreter and accepts no callables.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


FACTOR_IR_SCHEMA_VERSION = "veriquant.factor-ir/v2"
COMPILER_VERSION = "veriquant.static-formula-compiler/v2.0.0"


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class FactorIRFailureCode(str, Enum):
    EMPTY_SOURCE = "EMPTY_SOURCE"
    SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
    INVALID_SYNTAX = "INVALID_SYNTAX"
    FORBIDDEN_STATEMENT = "FORBIDDEN_STATEMENT"
    FORBIDDEN_NODE = "FORBIDDEN_NODE"
    UNKNOWN_NAME = "UNKNOWN_NAME"
    UNKNOWN_CALL = "UNKNOWN_CALL"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    FORBIDDEN_FIELD = "FORBIDDEN_FIELD"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    DYNAMIC_WINDOW = "DYNAMIC_WINDOW"
    FUTURE_REFERENCE = "FUTURE_REFERENCE"
    SSA_REASSIGNMENT = "SSA_REASSIGNMENT"
    SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"
    DEPTH_LIMIT_EXCEEDED = "DEPTH_LIMIT_EXCEEDED"
    SOURCE_KIND_UNSUPPORTED = "SOURCE_KIND_UNSUPPORTED"
    DRAFT_METADATA_MISMATCH = "DRAFT_METADATA_MISMATCH"
    POLICY_MISMATCH = "POLICY_MISMATCH"


class FactorIRCompileError(ValueError):
    def __init__(self, code: FactorIRFailureCode, message: str, **details: object) -> None:
        super().__init__(message)
        self.code = code
        self.details = MappingProxyType(dict(sorted(details.items())))


class IRValueKind(str, Enum):
    FIELD = "FIELD"
    CONSTANT = "CONSTANT"
    UNARY = "UNARY"
    BINARY = "BINARY"
    COMPARE = "COMPARE"
    BOOLEAN = "BOOLEAN"
    CALL = "CALL"
    CONDITIONAL = "CONDITIONAL"


@dataclass(frozen=True, slots=True)
class FactorIRNode:
    node_id: str
    kind: IRValueKind
    operator: str
    inputs: tuple[str, ...] = ()
    attributes: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.node_id.startswith("n") or not self.node_id[1:].isdigit():
            raise FactorIRCompileError(FactorIRFailureCode.INVALID_ARGUMENT, "invalid IR node identity")
        if not isinstance(self.kind, IRValueKind) or not self.operator:
            raise FactorIRCompileError(FactorIRFailureCode.INVALID_ARGUMENT, "invalid IR node")
        if any(callable(value) for value in self.attributes.values()):
            raise FactorIRCompileError(FactorIRFailureCode.FORBIDDEN_NODE, "callables are prohibited")
        object.__setattr__(self, "attributes", MappingProxyType(dict(sorted(self.attributes.items()))))

    def to_canonical(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "operator": self.operator,
            "inputs": list(self.inputs),
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class OperatorPolicy:
    operator: str
    policy_id: str
    version: str
    semantics: str

    @property
    def policy_hash(self) -> str:
        return canonical_hash(self.to_canonical())

    def to_canonical(self) -> dict[str, str]:
        return {"operator": self.operator, "policy_id": self.policy_id, "version": self.version, "semantics": self.semantics}


@dataclass(frozen=True, slots=True)
class SemanticAdapterPolicy:
    adapter: str
    policy_id: str
    version: str
    semantics: str
    policy_hash: str
    adapter_hash: str

    def to_canonical(self) -> dict[str, str]:
        return {
            "adapter": self.adapter,
            "policy_id": self.policy_id,
            "version": self.version,
            "semantics": self.semantics,
            "policy_hash": self.policy_hash,
            "adapter_hash": self.adapter_hash,
        }


@dataclass(frozen=True, slots=True)
class PostprocessStep:
    operation: str
    policy_id: str
    version: str
    parameters: Mapping[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(sorted(self.parameters.items()))))

    def to_canonical(self) -> dict[str, object]:
        return {"operation": self.operation, "policy_id": self.policy_id, "version": self.version, "parameters": dict(self.parameters)}


@dataclass(frozen=True, slots=True)
class ProgramIdentity:
    program_hash: str
    source_hash: str
    compiler_hash: str
    operator_policy_hash: str
    semantic_adapter_policy_hash: str
    postprocess_plan_hash: str
    schema_version: str = FACTOR_IR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != FACTOR_IR_SCHEMA_VERSION:
            raise FactorIRCompileError(FactorIRFailureCode.POLICY_MISMATCH, "FactorIR schema mismatch")
        for name in ("program_hash", "source_hash", "compiler_hash", "operator_policy_hash", "semantic_adapter_policy_hash", "postprocess_plan_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
                raise FactorIRCompileError(FactorIRFailureCode.INVALID_ARGUMENT, f"invalid {name}")

    def to_canonical(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in (
            "schema_version", "program_hash", "source_hash", "compiler_hash", "operator_policy_hash",
            "semantic_adapter_policy_hash", "postprocess_plan_hash",
        )}


@dataclass(frozen=True, slots=True)
class FactorIRProgram:
    nodes: tuple[FactorIRNode, ...]
    output_node: str
    required_fields: tuple[str, ...]
    lookback: int
    operator_policies: tuple[OperatorPolicy, ...]
    semantic_adapter_policies: tuple[SemanticAdapterPolicy, ...]
    postprocess_plan: tuple[PostprocessStep, ...]
    source_kind: str
    identity: ProgramIdentity
    schema_version: str = FACTOR_IR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.nodes or self.output_node not in {node.node_id for node in self.nodes}:
            raise FactorIRCompileError(FactorIRFailureCode.INVALID_ARGUMENT, "FactorIR output is missing")
        if tuple(sorted(set(self.required_fields))) != self.required_fields or self.lookback < 0:
            raise FactorIRCompileError(FactorIRFailureCode.INVALID_ARGUMENT, "invalid FactorIR metadata")
        seen: set[str] = set()
        for node in self.nodes:
            if node.node_id in seen or any(item not in seen for item in node.inputs):
                raise FactorIRCompileError(FactorIRFailureCode.INVALID_ARGUMENT, "FactorIR is not canonical SSA order")
            seen.add(node.node_id)
        if self.identity.program_hash != canonical_hash(self.identity_material()):
            raise FactorIRCompileError(FactorIRFailureCode.POLICY_MISMATCH, "FactorIR program identity mismatch")
        expected_operator_hash = canonical_hash([item.to_canonical() for item in self.operator_policies])
        expected_adapter_hash = canonical_hash([item.to_canonical() for item in self.semantic_adapter_policies])
        expected_postprocess_hash = canonical_hash([item.to_canonical() for item in self.postprocess_plan])
        if (
            self.identity.operator_policy_hash != expected_operator_hash
            or self.identity.semantic_adapter_policy_hash != expected_adapter_hash
            or self.identity.postprocess_plan_hash != expected_postprocess_hash
        ):
            raise FactorIRCompileError(FactorIRFailureCode.POLICY_MISMATCH, "FactorIR policy summary hash mismatch")

    def identity_material(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_kind": self.source_kind,
            "nodes": [node.to_canonical() for node in self.nodes],
            "output_node": self.output_node,
            "required_fields": list(self.required_fields),
            "lookback": self.lookback,
            "operator_policies": [item.to_canonical() for item in self.operator_policies],
            "semantic_adapter_policies": [item.to_canonical() for item in self.semantic_adapter_policies],
            "postprocess_plan": [item.to_canonical() for item in self.postprocess_plan],
            "source_hash": self.identity.source_hash,
            "compiler_hash": self.identity.compiler_hash,
        }

    @property
    def program_hash(self) -> str:
        return self.identity.program_hash

    @property
    def source_hash(self) -> str:
        return self.identity.source_hash

    @property
    def compiler_hash(self) -> str:
        return self.identity.compiler_hash

    def to_canonical(self) -> dict[str, object]:
        return {**self.identity_material(), "identity": self.identity.to_canonical()}


@dataclass(frozen=True, slots=True)
class CompilationFailure:
    code: FactorIRFailureCode
    message: str
    source_hash: str
    compiler_hash: str
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(sorted(self.details.items()))))


@dataclass(frozen=True, slots=True)
class CompilationResult:
    program: FactorIRProgram | None = None
    failure: CompilationFailure | None = None

    def __post_init__(self) -> None:
        if (self.program is None) == (self.failure is None):
            raise ValueError("exactly one of program or failure is required")

    @property
    def ok(self) -> bool:
        return self.program is not None
