"""Integration hook for the isolated VeriQuant-only deployment profile."""

from __future__ import annotations

from typing import Any, Mapping

from veriquant.v1_security.api import (
    ProbeReport,
    RouteAuthorityManifest,
    SecurityAcceptanceProbe,
    VeriQuantOfflineDeploymentProfile,
)


def run_veriquant_only_security_acceptance(
    *,
    manifest: RouteAuthorityManifest,
    workbench_payload: Mapping[str, Any],
    source_texts: Mapping[str, str],
    log_lines: tuple[str, ...] = ("profile=veriquant-offline-readonly",),
) -> ProbeReport:
    """Run the mechanical security gate with an empty provider environment."""

    profile = VeriQuantOfflineDeploymentProfile.default()

    return SecurityAcceptanceProbe(profile=profile).run_acceptance(

        manifest=manifest,
        llm_context={"visible_summary": "train/validation only"},
        workbench_payload=dict(workbench_payload),
        log_lines=log_lines,
        source_texts=dict(source_texts),
        env={},
    )

