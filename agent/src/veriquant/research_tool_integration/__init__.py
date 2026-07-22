"""Overnight integration harness for Campaign-routed research tools.

This package is deliberately product-layer only.  It wires READY overnight
packages through the CampaignToolRegistry and provides an offline demo.  It
does not register public API routes, modify the global veriquant package
exports, calculate official Evidence, decide Promotion, construct portfolios,
approve risk, create orders, or write ledgers.
"""

__all__ = [
    "INTEGRATION_SCHEMA_VERSION",
    "OvernightIntegrationError",
    "OvernightResearchToolDemo",
    "build_demo_artifacts",
    "load_ready_package_results",
    "main",
]


def __getattr__(name: str):
    if name == "INTEGRATION_SCHEMA_VERSION":
        from .demo import INTEGRATION_SCHEMA_VERSION

        return INTEGRATION_SCHEMA_VERSION
    if name == "OvernightIntegrationError":
        from .demo import OvernightIntegrationError

        return OvernightIntegrationError
    if name == "OvernightResearchToolDemo":
        from .demo import OvernightResearchToolDemo

        return OvernightResearchToolDemo
    if name == "build_demo_artifacts":
        from .demo import build_demo_artifacts

        return build_demo_artifacts
    if name == "load_ready_package_results":
        from .demo import load_ready_package_results

        return load_ready_package_results
    if name == "main":
        from .demo import main

        return main
    raise AttributeError(name)
