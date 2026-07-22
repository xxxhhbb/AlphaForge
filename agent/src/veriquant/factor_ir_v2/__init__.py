"""Public FactorIR v2 contracts (no execution surface)."""

from .models import (
    COMPILER_VERSION,
    FACTOR_IR_SCHEMA_VERSION,
    CompilationFailure,
    CompilationResult,
    FactorIRCompileError,
    FactorIRFailureCode,
    FactorIRNode,
    FactorIRProgram,
    IRValueKind,
    OperatorPolicy,
    PostprocessStep,
    ProgramIdentity,
    SemanticAdapterPolicy,
    canonical_hash,
    canonical_json,
)
from .policies import COMPILER_HASH

__all__ = [
    "COMPILER_HASH", "COMPILER_VERSION", "FACTOR_IR_SCHEMA_VERSION", "CompilationFailure", "CompilationResult",
    "FactorIRCompileError", "FactorIRFailureCode", "FactorIRNode", "FactorIRProgram", "IRValueKind",
    "OperatorPolicy", "PostprocessStep", "ProgramIdentity", "SemanticAdapterPolicy", "canonical_hash", "canonical_json",
]
