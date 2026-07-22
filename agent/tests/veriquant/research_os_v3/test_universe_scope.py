from __future__ import annotations

from veriquant.dynamic_data_planning import UniverseKind
from veriquant.research_os_v3.product_service import _research_universe_spec


def test_generic_ashare_research_uses_an_explicit_historical_index_scope() -> None:
    scope = _research_universe_spec(
        "研究价格与成交量 Alpha",
        ("CN_ASHARE_APPROVED_PIT",),
    )

    assert scope.kind is UniverseKind.HISTORICAL_INDEX_CONSTITUENTS
    assert scope.index_code == "000300.SH"


def test_explicit_all_market_question_is_not_silently_downgraded() -> None:
    scope = _research_universe_spec(
        "研究全部 A 股的横截面质量因子",
        ("CN_ASHARE_APPROVED_PIT",),
    )

    assert scope.kind is UniverseKind.HISTORICAL_ALL_A_SHARE


def test_named_index_is_preserved_in_data_plan() -> None:
    scope = _research_universe_spec("研究中证500反转", ("CN_ASHARE_APPROVED_PIT",))

    assert scope.kind is UniverseKind.HISTORICAL_INDEX_CONSTITUENTS
    assert scope.index_code == "000905.SH"
