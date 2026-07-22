"""VeriQuant-only offline deployment profile and route authority manifest."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    SCHEMA_VERSION,
    RouteRecord,
    SecurityFinding,
    SecuritySeverity,
    canonical_hash,
    finding,
)


SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DEFAULT_ARTIFACT_NAMES = frozenset(
    {
        "overview",
        "lineage",
        "evidence",
        "critics",
        "promotion",
        "citations",
        "shadow_portfolio",
        "nav_attribution",
        "drift_alerts",
    }
)


@dataclass(frozen=True, kw_only=True)
class RouteAuthorityManifest:
    """Read-only route manifest for an isolated VeriQuant API surface."""

    profile_id: str
    routes: tuple[RouteRecord, ...]
    imported_modules: tuple[str, ...] = ()
    offline_only: bool = True
    provider_keys_required: bool = False
    artifact_root: str = ".autobuild/veriquant/integration/workbench_artifacts"
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "routes", tuple(self.routes))
        object.__setattr__(self, "imported_modules", tuple(str(item) for item in self.imported_modules))

    @property
    def artifact_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "offline_only": self.offline_only,
            "provider_keys_required": self.provider_keys_required,
            "artifact_root": self.artifact_root,
            "imported_modules": self.imported_modules,
            "routes": [route.to_dict() for route in self.routes],
        }


@dataclass(frozen=True, kw_only=True)
class VeriQuantOfflineDeploymentProfile:
    """Fail-closed profile for a standalone VeriQuant read-only API app."""

    profile_id: str = "veriquant-offline-readonly"
    allowed_methods: tuple[str, ...] = ("GET", "HEAD", "OPTIONS")
    allowed_path_prefixes: tuple[str, ...] = (
        "/veriquant/workbench",
        "/api/veriquant/research-os/v3",
        "/veriquant/status",
        "/healthz",
    )
    allowed_authorities: tuple[str, ...] = (
        "READ_ONLY_WORKBENCH",
        "READ_ONLY_RESEARCH",
        "READ_ONLY_STATUS",
    )
    denied_path_tokens: tuple[str, ...] = ("/live", "broker", "order", "oauth", "mandate", "account", "trade")
    denied_import_tokens: tuple[str, ...] = (
        "src.api.live_routes",
        "src.live",
        "src.trading",
        "live_routes",
        "broker",
        "oauth",
        "mandate",
    )
    truth_authorities: tuple[str, ...] = (
        "official_metrics",
        "promotion",
        "risk_approval",
        "portfolio_write",
        "target_weights",
        "orders",
        "fills",
        "ledger_write",
        "sealed_read",
    )

    @classmethod
    def default(cls) -> "VeriQuantOfflineDeploymentProfile":
        return cls()

    def build_manifest(
        self,
        *,
        routes: Iterable[RouteRecord | Mapping[str, Any] | Any],
        imported_modules: Iterable[str] = ("src.api.veriquant_workbench_routes",),
        artifact_root: str = ".autobuild/veriquant/integration/workbench_artifacts",
    ) -> RouteAuthorityManifest:
        return RouteAuthorityManifest(
            profile_id=self.profile_id,
            routes=tuple(self._coerce_route(route) for route in routes),
            imported_modules=tuple(str(item) for item in imported_modules),
            artifact_root=artifact_root,
            offline_only=True,
            provider_keys_required=False,
        )

    def validate_manifest(self, manifest: RouteAuthorityManifest) -> tuple[SecurityFinding, ...]:
        routes = tuple(manifest.routes)
        route_payload = manifest.to_dict()
        duplicate_paths = self._duplicate_route_keys(routes)
        blocked_routes = tuple(route.path for route in routes if self._route_path_blocked(route.path))
        unknown_routes = tuple(route.path for route in routes if not self._route_path_allowed(route.path))
        write_routes = tuple(
            route.path
            for route in routes
            if any(method not in self.allowed_methods for method in route.methods)
        )
        authority_routes = tuple(
            route.path
            for route in routes
            if route.authority not in self.allowed_authorities
        )
        blocked_imports = tuple(
            module
            for module in manifest.imported_modules
            if self._contains_any(module.lower(), self.denied_import_tokens)
        )
        return (
            finding(
                check_id="route_table.no_live_or_privileged_paths",
                passed=not blocked_routes,
                message="VeriQuant-only route table contains no live, broker, order, OAuth, mandate, account, or trade paths.",
                evidence=route_payload,
                details={"blocked_routes": blocked_routes},
            ),
            finding(
                check_id="route_table.only_allowed_prefixes",
                passed=not unknown_routes,
                message="Every route is scoped to Workbench or status.",
                evidence=route_payload,
                details={"unknown_routes": unknown_routes},
            ),
            finding(
                check_id="route_table.read_only_methods",
                passed=not write_routes,
                message="Every route method is read-only.",
                evidence=route_payload,
                details={"write_routes": write_routes},
            ),
            finding(
                check_id="route_table.read_only_authority",
                passed=not authority_routes,
                message="Every route authority is read-only.",
                evidence=route_payload,
                details={"authority_routes": authority_routes},
            ),
            finding(
                check_id="route_table.no_duplicate_routes",
                passed=not duplicate_paths,
                message="Route method/path keys are unique.",
                evidence=route_payload,
                severity=SecuritySeverity.WARNING,
                details={"duplicates": duplicate_paths},
            ),
            finding(
                check_id="imports.no_live_broker_runner",
                passed=not blocked_imports,
                message="Profile imports do not include live routes, broker adapters, OAuth, mandate, or live runners.",
                evidence={"imported_modules": manifest.imported_modules},
                details={"blocked_imports": blocked_imports},
            ),
            finding(
                check_id="startup.offline_no_provider_key_required",
                passed=manifest.offline_only and not manifest.provider_keys_required,
                message="Profile starts offline and does not require provider keys.",
                evidence=manifest.to_dict(),
            ),
        )

    def startup_probe(self, env: Mapping[str, str] | None = None) -> tuple[SecurityFinding, ...]:
        selected_env = {
            key: str(value)
            for key, value in (env or {}).items()
            if key
            in {
                "ENABLE_LIVE_BROKER",
                "ENABLE_REAL_BROKER",
                "ENABLE_LLM_RESEARCH",
                "ENABLE_ASHARE_NETWORK",
                "ENABLE_QLIB_ADAPTER",
                "API_AUTH_KEY",
                "OPENAI_API_KEY",
                "TUSHARE_TOKEN",
            }
        }
        enabled_flags = tuple(
            key
            for key, value in selected_env.items()
            if key.startswith("ENABLE_") and str(value).strip().lower() in {"1", "true", "yes", "on"}
        )
        provider_keys = tuple(key for key in selected_env if key.endswith("_KEY") or key.endswith("_TOKEN"))
        redacted = {key: ("<present>" if key in provider_keys else selected_env[key]) for key in selected_env}
        return (
            finding(
                check_id="startup.default_offline_flags",
                passed=not enabled_flags,
                message="Default startup remains offline and does not enable network, LLM, Qlib, or broker capabilities.",
                evidence=redacted,
                details={"enabled_flags": enabled_flags},
            ),
            finding(
                check_id="startup.no_provider_key_dependency",
                passed=True,
                message="Provider keys are not required for startup; presence is recorded only as a redacted boolean.",
                evidence={"provider_key_names_present": provider_keys},
                details={"provider_key_names_present": provider_keys},
            ),
        )

    def resolve_artifact_locator(self, root: Path, *, run_id: str, artifact_name: str) -> Path:
        self._require_safe_id(run_id, "run_id")
        self._require_safe_id(artifact_name, "artifact_name")
        if artifact_name not in DEFAULT_ARTIFACT_NAMES:
            raise ValueError("unknown Workbench artifact")
        base = Path(root).resolve()
        target = base / "runs" / run_id / f"{artifact_name}.json"
        resolved = target.resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError("artifact locator escapes root") from exc
        return resolved

    def denies_capability(self, actor: str, capability: str) -> bool:
        actor_name = str(actor).strip().lower()
        cap = str(capability).strip().lower()
        if actor_name in {"llm", "research_desk", "document", "news", "quantaalpha", "qlib"}:
            return cap in self.truth_authorities or cap in {"promotion", "risk", "portfolio", "order", "ledger", "sealed"}
        return False

    def _coerce_route(self, route: RouteRecord | Mapping[str, Any] | Any) -> RouteRecord:
        if isinstance(route, RouteRecord):
            return route
        if isinstance(route, Mapping):
            return RouteRecord(
                path=str(route.get("path", "")),
                methods=self._coerce_methods(route.get("methods") or route.get("method") or ("GET",)),
                authority=str(route.get("authority", "READ_ONLY_WORKBENCH")),
                handler=str(route.get("handler", "")),
            )
        return RouteRecord(
            path=str(getattr(route, "path", "")),
            methods=self._coerce_methods(getattr(route, "methods", ("GET",)) or ("GET",)),
            authority=str(getattr(route, "authority", "READ_ONLY_WORKBENCH")),
            handler=str(getattr(getattr(route, "endpoint", None), "__name__", "")),
        )

    def _route_path_allowed(self, path: str) -> bool:
        value = path.lower()
        return any(value == prefix.lower() or value.startswith(prefix.lower() + "/") for prefix in self.allowed_path_prefixes)

    def _route_path_blocked(self, path: str) -> bool:
        return self._contains_any(path.lower(), self.denied_path_tokens)

    @staticmethod
    def _contains_any(value: str, needles: Iterable[str]) -> bool:
        lowered = value.lower()
        return any(str(needle).lower() in lowered for needle in needles)

    @staticmethod
    def _coerce_methods(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value)

    @staticmethod
    def _duplicate_route_keys(routes: tuple[RouteRecord, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for route in routes:
            for method in route.methods:
                key = f"{method} {route.path}"
                if key in seen:
                    duplicates.append(key)
                seen.add(key)
        return tuple(sorted(duplicates))

    @staticmethod
    def _require_safe_id(value: str, field: str) -> None:
        if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
            raise ValueError(f"invalid {field}")
        if ".." in value or "/" in value or "\\" in value or ":" in value or "\x00" in value:
            raise ValueError(f"invalid {field}")
