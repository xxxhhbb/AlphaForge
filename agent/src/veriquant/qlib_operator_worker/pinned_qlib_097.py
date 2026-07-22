"""Fixed pyqlib 0.9.7 bindings. Import only after runtime hash preflight."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Sequence

import pandas as pd
from qlib.data.base import Expression
from qlib.data.cache import H
from qlib.data.ops import (
    Abs,
    Add,
    And,
    ChangeInstrument,
    Corr,
    Count,
    Cov,
    Delta,
    Div,
    EMA,
    Eq,
    Ge,
    Greater,
    Gt,
    IdxMax,
    IdxMin,
    If,
    Kurt,
    Le,
    Less,
    Log,
    Lt,
    Mad,
    Mask,
    Max,
    Mean,
    Med,
    Min,
    Mul,
    Ne,
    Not,
    Or,
    Power,
    Quantile,
    Rank,
    Ref,
    Resi,
    Rsquare,
    Sign,
    Skew,
    Slope,
    Std,
    Sub,
    Sum,
    Var,
    WMA,
)


BUILTIN_OPERATOR_CLASSES = MappingProxyType(
    {
        "Abs": Abs, "Add": Add, "And": And, "ChangeInstrument": ChangeInstrument, "Corr": Corr, "Count": Count,
        "Cov": Cov, "Delta": Delta, "Div": Div, "EMA": EMA, "Eq": Eq, "Ge": Ge, "Greater": Greater, "Gt": Gt,
        "IdxMax": IdxMax, "IdxMin": IdxMin, "If": If, "Kurt": Kurt, "Le": Le, "Less": Less, "Log": Log,
        "Lt": Lt, "Mad": Mad, "Mask": Mask, "Max": Max, "Mean": Mean, "Med": Med, "Min": Min, "Mul": Mul,
        "Ne": Ne, "Not": Not, "Or": Or, "Power": Power, "Quantile": Quantile, "Rank": Rank, "Ref": Ref,
        "Resi": Resi, "Rsquare": Rsquare, "Sign": Sign, "Skew": Skew, "Slope": Slope, "Std": Std, "Sub": Sub,
        "Sum": Sum, "Var": Var, "WMA": WMA,
    }
)


class InMemorySeriesLeaf(Expression):
    """Provider-free leaf; it is not registered as a Qlib custom operator."""

    def __init__(self, field: str, values_by_symbol: Mapping[str, Sequence[float]], namespace: str) -> None:
        self.field = field
        self.namespace = namespace
        self.values_by_symbol = {
            symbol: pd.Series(tuple(values), index=pd.RangeIndex(len(values)), dtype="float64")
            for symbol, values in values_by_symbol.items()
        }

    def __str__(self) -> str:
        return f"MemoryField({self.namespace},{self.field})"

    def _load_internal(self, instrument, start_index, end_index, *args):
        if instrument not in self.values_by_symbol:
            raise KeyError("in-memory instrument unavailable")
        if not isinstance(start_index, int) or not isinstance(end_index, int):
            raise ValueError("in-memory leaf requires integer range")
        index = pd.RangeIndex(start_index, end_index + 1)
        return self.values_by_symbol[instrument].reindex(index).copy()

    def get_longest_back_rolling(self):
        return 0

    def get_extended_window_size(self):
        return 0, 0


def clear_expression_cache() -> None:
    H.clear()
