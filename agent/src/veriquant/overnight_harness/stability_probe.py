"""Mechanical non-test gates for the AlphaForge product stability Harness."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

from .models import Phase, SCHEMA_VERSION


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _direct_execution_hits(root: Path) -> tuple[str, ...]:
    hits: list[str] = []
    trusted = (
        root / "agent/src/veriquant/research_os_v3",
        root / "agent/src/veriquant/specialist_research",
        root / "agent/src/veriquant/dynamic_data_integration",
        root / "agent/src/veriquant/formal_report_v2",
    )
    for directory in trusted:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            try:
                tree = ast.parse(_read(path), filename=str(path))
            except (OSError, SyntaxError):
                hits.append(str(path.relative_to(root)))
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in {"eval", "exec", "compile"}:
                        hits.append(f"{path.relative_to(root)}:{node.lineno}:{node.func.id}")
    return tuple(hits)


def _check(phase: Phase, repo: Path) -> tuple[str, dict[str, object]]:
    if phase is Phase.DISCOVER:
        required = (
            "AGENT_CONTRIBUTOR_GUIDE.md",
            ".autobuild/veriquant/BUILD_CONSTITUTION.md",
            ".autobuild/veriquant/state.json",
            "agent/alphaforge_api_server.py",
            "agent/src/veriquant/research_os_v3/product_service.py",
            "docs/veriquant/PRODUCT_ACCEPTANCE_REPAIR24.md",
        )
        missing = [item for item in required if not (repo / item).is_file()]
        residues = [str(path.relative_to(repo)) for path in (repo / "agent/src/veriquant").rglob("*.orig")]
        return ("READY" if not missing and not residues else "FAILED", {"missing": missing, "patch_residues": residues})

    if phase is Phase.PLAN:
        central = _read(repo / "agent/api_server.py")
        product = _read(repo / "agent/alphaforge_api_server.py")
        routes = _read(repo / "agent/src/api/research_os_v3_routes.py")
        failures: list[str] = []
        if "include_router(research_os_v2_router" in central:
            failures.append("LEGACY_V2_ROUTE_REGISTERED")
        if "research_os_v2_router" in product or "alphaforge_campaign_router" in product:
            failures.append("LEGACY_PRODUCT_RUNTIME_REGISTERED")
        if "research_os_v3_control_router" not in product or "research_os_v3_router" not in product:
            failures.append("V3_PRODUCT_ROUTES_MISSING")
        if "include_router(research_os_v3_router" not in central:
            failures.append("V3_READ_ROUTE_MISSING")
        if "ResearchOSV3ProductService" not in routes:
            failures.append("CANONICAL_PRODUCT_SERVICE_MISSING")
        if "ResearchRunManifest" not in _read(repo / "agent/src/veriquant/research_os_v3/product_service.py"):
            failures.append("CANONICAL_MANIFEST_MISSING")
        return ("READY" if not failures else "FAILED", {"failures": failures})

    if phase is Phase.IMPLEMENT:
        from agent.loop import AgentLoop
        from backtest.loaders.tushare_fundamentals import TushareFundamentalProvider
        from swarm.runtime import SwarmRuntime
        from veriquant.dynamic_data_integration import CampaignEvidenceDatasetBuilder
        from veriquant.research_os_v3.product_service import ResearchOSV3ProductService

        symbols = tuple(
            item.__name__
            for item in (
                AgentLoop,
                SwarmRuntime,
                TushareFundamentalProvider,
                CampaignEvidenceDatasetBuilder,
                ResearchOSV3ProductService,
            )
        )
        return "READY", {"reusable_symbols": symbols, "trusted_product_service": symbols[-1]}

    if phase is Phase.INDEPENDENT_REVIEW:
        import alphaforge_api_server
        import veriquant_api_server

        app = veriquant_api_server.create_app()
        manifest = veriquant_api_server.build_route_manifest(app)
        forbidden = tuple(
            route.path
            for route in manifest.routes
            if any(token in route.path.lower() for token in ("/live", "broker", "order", "oauth", "mandate"))
        )
        execution_hits = _direct_execution_hits(repo)
        product_paths = tuple(
            str(route.path).lower()
            for route in alphaforge_api_server._iter_routes(alphaforge_api_server.app.routes)
        )
        product_forbidden = tuple(
            path for path in product_paths
            if "/research-os/v2" in path
            or any(token in path for token in ("/live", "broker", "order", "oauth", "mandate"))
        )
        good = (
            manifest.offline_only
            and not manifest.provider_keys_required
            and not forbidden
            and not product_forbidden
            and any("/research-os/v3/plans" in path for path in product_paths)
            and not execution_hits
        )
        return (
            "READY" if good else "FAILED",
            {
                "route_manifest_hash": manifest.artifact_hash,
                "forbidden_routes": forbidden,
                "product_forbidden_routes": product_forbidden,
                "direct_execution_hits": execution_hits,
            },
        )

    if phase is Phase.COMMIT_CHECKPOINT:
        required = (
            repo / "docs/veriquant/PRODUCT_ACCEPTANCE_REPAIR24.md",
            repo / "docs/veriquant/ARCHITECTURE.md",
            repo / "docs/veriquant/THREAT_MODEL.md",
            repo / "frontend/dist/index.html",
        )
        missing = [str(path.relative_to(repo)) for path in required if not path.is_file()]
        launch_page = _read(repo / "frontend/src/pages/ResearchOSV3LaunchPage.tsx")
        launch_client = _read(repo / "frontend/src/features/research-os-v3-launch/client.ts")
        api_routes = _read(repo / "agent/src/api/research_os_v3_routes.py")
        built_javascript = "\n".join(
            _read(path)
            for path in sorted((repo / "frontend/dist/assets").glob("*.js"))
        ) if (repo / "frontend/dist/assets").is_dir() else ""
        markers = {
            "launch_calls_prepare_data": "preparePlanData" in launch_page,
            "client_uses_v3_prepare_endpoint": "/prepare-data" in launch_client,
            "api_returns_updated_plan": 'response["plan"]' in api_routes,
            "built_ui_exposes_explicit_prepare_action": "准备真实研究数据" in built_javascript,
        }
        good = not missing and all(markers.values())
        return ("READY" if good else "FAILED", {"missing": missing, "frontend_data_preparation": markers})

    if phase is Phase.NEXT_PHASE:
        path = repo / ".autobuild/veriquant/product_acceptance/repair24/acceptance_final/acceptance_report.json"
        payload = json.loads(_read(path)) if path.is_file() else {}
        verdicts = payload.get("verdicts") if isinstance(payload.get("verdicts"), list) else []
        terminals: dict[str, int] = {}
        for item in verdicts:
            if isinstance(item, dict):
                terminal = str(item.get("terminal") or "MISSING")
                terminals[terminal] = terminals.get(terminal, 0) + 1
        good = (
            payload.get("status") == "PASSED"
            and payload.get("scenario_count") == 24
            and payload.get("passed_count") == 24
            and payload.get("real_or_registered_count") == 24
            and not payload.get("global_failure_codes")
            and len(verdicts) == 24
            and terminals.get("OFFICIAL_EVALUATED", 0) >= 1
            and not any(name in terminals for name in ("SYSTEM_FAILED", "MISSING"))
        )
        return (
            "READY" if good else "FAILED",
            {
                "status": payload.get("status"),
                "scenario_count": payload.get("scenario_count"),
                "passed_count": payload.get("passed_count"),
                "terminal_counts": dict(sorted(terminals.items())),
            },
        )

    return "READY", {"phase": phase.value}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=[item.value for item in Phase])
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args()
    phase = Phase(args.phase)
    status, evidence = _check(phase, args.repo_root.resolve())
    print(json.dumps({"schema_version": SCHEMA_VERSION, "phase": phase.value, "status": status, "evidence": evidence}, ensure_ascii=False, sort_keys=True))
    return 0 if status == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
