from __future__ import annotations

import pytest

from src.veriquant.capability_catalog import (
    CapabilityAvailability,
    DataSourceLabel,
    ResearchCapabilitySpec,
    build_default_research_capability_catalog,
)
from src.veriquant.contracts import ContractValidationError


def test_catalog_uses_code_probe_not_documentation_claims() -> None:
    catalog = build_default_research_capability_catalog(
        extra_specs=(
            ResearchCapabilitySpec(
                capability_id="docs.only.fake",
                title_zh="文档声明的虚构材料",
                module_path="src.veriquant.no_such_package",
                symbol_name="NoSuchSymbol",
                tool_name="fake_material",
                provider_source_label=DataSourceLabel.AUXILIARY_ONLY,
                allowed_data=("price_volume",),
                fields=("close",),
                pit_safe=True,
                online_required=False,
            ),
        )
    )

    fake = catalog.require("docs.only.fake")
    real = catalog.require("frozen.market.summary")

    assert fake.availability is CapabilityAvailability.UNAVAILABLE
    assert fake.probe.implemented is False
    assert real.availability is CapabilityAvailability.AVAILABLE
    assert real.probe.implemented is True
    assert real.probe.source_file_hash is not None
    assert real.probe.source_file_hash.startswith("sha256:")


def test_approved_proxy_is_online_required_and_never_official_tushare() -> None:
    catalog = build_default_research_capability_catalog()
    proxy = catalog.require("approved.proxy.daily")

    assert proxy.provider_source_label is DataSourceLabel.APPROVED_PROXY_DATA_VENDOR
    assert proxy.availability is CapabilityAvailability.ONLINE_REQUIRED
    assert proxy.online_required is True
    assert "OFFICIAL_TUSHARE" not in proxy.canonical_json()


def test_available_for_data_respects_allowed_data_and_stable_hash() -> None:
    first = build_default_research_capability_catalog()
    second = build_default_research_capability_catalog()

    available = first.available_for_data(("price_volume",))
    ids = {item.capability_id for item in available}

    assert "frozen.market.summary" in ids
    assert "frozen.fundamentals.summary" not in ids
    assert first.artifact_hash == second.artifact_hash


def test_catalog_rejects_secret_or_official_proxy_labels() -> None:
    with pytest.raises(ContractValidationError):
        ResearchCapabilitySpec(
            capability_id="bad.secret",
            title_zh="token 泄露",
            module_path="src.veriquant.vibe_market_adapters.adapters",
            symbol_name="FrozenMarketAdapter",
            tool_name="frozen_market_summary",
            provider_source_label=DataSourceLabel.FROZEN_VIBE_MATERIAL,
            allowed_data=("price_volume",),
            fields=("close",),
            pit_safe=True,
            online_required=False,
        )

    good = build_default_research_capability_catalog().require("approved.proxy.daily")
    with pytest.raises(ValueError):
        DataSourceLabel("OFFICIAL_TUSHARE")
    assert "OFFICIAL_TUSHARE" not in good.to_dict()["provider_source_label"]


def test_online_spec_remains_marked_online_required_even_when_probed() -> None:
    base = build_default_research_capability_catalog()
    online = base.require("vibe.loader.market_data")
    assert online.availability is CapabilityAvailability.ONLINE_REQUIRED

    explicit_online_catalog = build_default_research_capability_catalog(offline_default=False)
    explicit_online = explicit_online_catalog.require("vibe.loader.market_data")
    assert explicit_online.availability is CapabilityAvailability.ONLINE_REQUIRED
    assert explicit_online.online_required is True
