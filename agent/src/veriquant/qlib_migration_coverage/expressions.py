"""Audited Qlib158 expression templates; no legacy module is imported or called."""

from __future__ import annotations

import re
from dataclasses import dataclass


_WINDOWED = re.compile(r"^qlib158_([a-z]+)(5|10|20|30|60)$")
_EPSILON = "0.000000000001"


@dataclass(frozen=True, slots=True)
class ExpressionTemplate:
    expression: str
    composite_operators: tuple[str, ...] = ()


def _safe_div(numerator: str, denominator: str) -> str:
    """Represent legacy safe_div including NaN at an exact zero denominator."""

    adjusted = f"(({denominator})+{_EPSILON}*Sign({denominator}))"
    return f"If(Abs({denominator})>0,({numerator})/{adjusted},0/0)"


def qlib158_template(factor_id: str) -> ExpressionTemplate | None:
    """Return a reviewed structured formula template for a Qlib158 ID."""

    kbar = {
        "qlib158_klen": _safe_div("$high-$low", "$open"),
        "qlib158_kmid": _safe_div("$close-$open", "$open"),
        "qlib158_kmid2": _safe_div("$close-$open", "$high-$low"),
        "qlib158_ksft": _safe_div("2*$close-$high-$low", "$open"),
        "qlib158_ksft2": _safe_div("2*$close-$high-$low", "$high-$low"),
        "qlib158_kup": _safe_div("$high-If($open>$close,$open,$close)", "$open"),
        "qlib158_kup2": _safe_div("$high-If($open>$close,$open,$close)", "$high-$low"),
        "qlib158_klow": _safe_div("If($open<$close,$open,$close)-$low", "$open"),
        "qlib158_klow2": _safe_div("If($open<$close,$open,$close)-$low", "$high-$low"),
    }
    if factor_id in kbar:
        return ExpressionTemplate(kbar[factor_id], ("safe_div",))
    match = _WINDOWED.fullmatch(factor_id)
    if match is None:
        return None
    family, raw_window = match.groups()
    window = int(raw_window)
    delta_close = "Delta($close,1)"
    delta_volume = "Delta($volume,1)"
    safe = False
    expressions = {
        "beta": _safe_div(f"$close-Ref($close,{window})", "$close") + f"/{window}",
        "cntp": f"Mean(If($close>Ref($close,1),1,0),{window})",
        "cntn": f"Mean(If($close<Ref($close,1),1,0),{window})",
        "cntd": f"Mean(If($close>Ref($close,1),1,0),{window})-Mean(If($close<Ref($close,1),1,0),{window})",
        "corr": f"Corr($close,Log($volume+1),{window})",
        "cord": f"Corr({_safe_div('$close', 'Ref($close,1)')},Log({_safe_div('$volume+1', 'Ref($volume,1)+1')}),{window})",
        "imax": f"IdxMax($high,{window})/{window}",
        "imin": f"IdxMin($low,{window})/{window}",
        "imxd": f"(IdxMax($high,{window})-IdxMin($low,{window}))/{window}",
        "ma": _safe_div(f"Mean($close,{window})", "$close"),
        "max": _safe_div(f"Max($high,{window})", "$close"),
        "min": _safe_div(f"Min($low,{window})", "$close"),
        "qtld": _safe_div(f"Quantile($close,{window},0.2)", "$close"),
        "qtlu": _safe_div(f"Quantile($close,{window},0.8)", "$close"),
        "rank": f"Rank($close,{window})",
        "resi": _safe_div(f"$close-Mean($close,{window})", "$close"),
        "roc": _safe_div("$close", f"Ref($close,{window})") + "-1",
        "rsqr": f"Rsquare($close,{window})",
        "rsv": _safe_div(
            f"$close-Min($low,{window})", f"Max($high,{window})-Min($low,{window})"
        ),
        "std": _safe_div(f"Std($close,{window})", "$close"),
        "sump": _safe_div(
            f"Sum(If({delta_close}>0,{delta_close},0),{window})",
            f"Sum(Abs({delta_close}),{window})",
        ),
        "sumn": _safe_div(
            f"Sum(If({delta_close}<0,0-{delta_close},0),{window})",
            f"Sum(Abs({delta_close}),{window})",
        ),
        "sumd": _safe_div(
            f"Sum(If({delta_close}>0,{delta_close},0),{window})-Sum(If({delta_close}<0,0-{delta_close},0),{window})",
            f"Sum(Abs({delta_close}),{window})",
        ),
        "vma": _safe_div(f"Mean($volume,{window})", f"$volume+{_EPSILON}"),
        "vstd": _safe_div(f"Std($volume,{window})", f"$volume+{_EPSILON}"),
        "vsump": _safe_div(
            f"Sum(If({delta_volume}>0,{delta_volume},0),{window})",
            f"Sum(Abs({delta_volume}),{window})",
        ),
        "vsumn": _safe_div(
            f"Sum(If({delta_volume}<0,0-{delta_volume},0),{window})",
            f"Sum(Abs({delta_volume}),{window})",
        ),
        "vsumd": _safe_div(
            f"Sum(If({delta_volume}>0,{delta_volume},0),{window})-Sum(If({delta_volume}<0,0-{delta_volume},0),{window})",
            f"Sum(Abs({delta_volume}),{window})",
        ),
    }
    if family == "wvma":
        ret = _safe_div(delta_close, "Ref($close,1)")
        expression = _safe_div(
            f"Std(({ret})*$volume,{window})",
            f"Mean(Abs({ret})*$volume,{window})",
        )
        return ExpressionTemplate(expression, ("safe_div",))
    expression = expressions.get(family)
    if expression is None:
        return None
    safe = family in {
        "beta",
        "cord",
        "ma",
        "max",
        "min",
        "qtld",
        "qtlu",
        "resi",
        "roc",
        "rsv",
        "std",
        "sump",
        "sumn",
        "sumd",
        "vma",
        "vstd",
        "vsump",
        "vsumn",
        "vsumd",
    }
    return ExpressionTemplate(expression, ("safe_div",) if safe else ())


SEMANTICS_PARITY_OPERATORS = frozenset(
    {"IdxMax", "IdxMin", "Quantile", "Rank", "Rsquare"}
)
