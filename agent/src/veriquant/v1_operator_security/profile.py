"""Exact local-offline route and request policy for the UI operator plane."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .models import RouteAuthority, RouteAuthorityManifest, RouteRecord, SecurityFinding, finding


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
JOB_ID_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
ALLOWED_CREATE_FIELDS = frozenset({"profile_id", "seed", "budget", "idempotency_key"})
FORBIDDEN_INPUT_TOKENS = (
    "path", "url", "uri", "token", "secret", "authorization", "cookie", "credential",
    "code", "python", "shell", "sql", "exec", "eval", "provider", "stage",
    "official", "metric", "promotion", "risk", "portfolio", "weight", "position",
    "order", "fill", "broker", "ledger", "sealed", "oauth", "mandate",
)


def _expected_routes() -> tuple[RouteRecord, ...]:
    read_status = RouteAuthority.READ_ONLY_STATUS
    read_workbench = RouteAuthority.READ_ONLY_WORKBENCH
    return (
        RouteRecord(method="GET", path="/healthz", authority=read_status),
        RouteRecord(method="GET", path="/veriquant/status", authority=read_status),
        RouteRecord(method="GET", path="/veriquant/operator/profiles", authority=RouteAuthority.READ_ONLY_OPERATOR_CATALOG),
        RouteRecord(method="GET", path="/veriquant/workbench/runs", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}/lineage", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}/evidence", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}/critics", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}/promotion", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}/citations", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}/shadow-portfolio", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}/nav-attribution", authority=read_workbench),
        RouteRecord(method="GET", path="/veriquant/workbench/runs/{run_id}/drift-alerts", authority=read_workbench),
        RouteRecord(method="POST", path="/veriquant/jobs", authority=RouteAuthority.OFFLINE_JOB_CREATE, operator_auth_required=True),
        RouteRecord(method="GET", path="/veriquant/jobs/{job_id}", authority=RouteAuthority.OFFLINE_JOB_STATUS),
        RouteRecord(method="GET", path="/veriquant/jobs/{job_id}/events", authority=RouteAuthority.OFFLINE_JOB_EVENTS),
        RouteRecord(method="POST", path="/veriquant/jobs/{job_id}/cancel", authority=RouteAuthority.OFFLINE_JOB_CONTROL, operator_auth_required=True),
        RouteRecord(method="POST", path="/veriquant/jobs/{job_id}/resume", authority=RouteAuthority.OFFLINE_JOB_CONTROL, operator_auth_required=True),
    )


@dataclass(frozen=True, kw_only=True)
class LocalOfflineOperatorProfile:
    """Policy for one loopback deployment containing Workbench and operator routes.

    POST means orchestration control only. It never grants evidence, promotion,
    risk, portfolio, order, fill, or ledger authority.
    """

    profile_id: str = "veriquant-local-offline-operator/v1"
    bind_host: str = "127.0.0.1"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    allowed_origins: tuple[str, ...] = ("http://127.0.0.1:5899", "http://localhost:5899")
    expected_routes: tuple[RouteRecord, ...] = _expected_routes()
    denied_import_tokens: tuple[str, ...] = (
        "live_routes", "src.live", "src.trading", "broker", "oauth", "mandate",
        "account", "live_runner", "order_router",
    )

    @classmethod
    def default(cls) -> "LocalOfflineOperatorProfile":
        return cls()

    def build_manifest(
        self,
        *,
        routes: Iterable[RouteRecord],
        imported_modules: Iterable[str],
        bind_host: str | None = None,
    ) -> RouteAuthorityManifest:
        return RouteAuthorityManifest(
            profile_id=self.profile_id,
            routes=tuple(routes),
            bind_host=bind_host or self.bind_host,
            allowed_origins=self.allowed_origins,
            imported_modules=tuple(imported_modules),
        )

    def exact_manifest(self) -> RouteAuthorityManifest:
        return self.build_manifest(
            routes=self.expected_routes,
            imported_modules=(
                "src.api.veriquant_workbench_routes",
                "src.veriquant.operator.router",
                "src.veriquant.operator.service",
            ),
        )

    def validate_manifest(self, manifest: RouteAuthorityManifest) -> tuple[SecurityFinding, ...]:
        actual = {route.key: route for route in manifest.routes}
        expected = {route.key: route for route in self.expected_routes}
        missing = tuple(sorted(set(expected) - set(actual)))
        extra = tuple(sorted(set(actual) - set(expected)))
        mismatched = tuple(
            sorted(
                key for key in set(actual) & set(expected)
                if actual[key].to_dict() != expected[key].to_dict()
            )
        )
        blocked_imports = tuple(
            sorted(module for module in manifest.imported_modules if self._contains_any(module, self.denied_import_tokens))
        )
        financial = tuple(sorted(route.key for route in manifest.routes if route.financial_authority))
        unsafe_without_auth = tuple(
            sorted(route.key for route in manifest.routes if route.method == "POST" and not route.operator_auth_required)
        )
        bind_ok = self._host_name(manifest.bind_host) in set(self.allowed_hosts)
        origins_ok = tuple(manifest.allowed_origins) == tuple(sorted(self.allowed_origins))
        base_evidence = manifest.to_dict()
        return (
            finding(
                check_id="routes.exact_allowlist",
                passed=not missing and not extra and not mismatched,
                message="Route table exactly matches the versioned Workbench and operator allowlist.",
                evidence=base_evidence,
                details={"missing": missing, "extra": extra, "mismatched": mismatched},
            ),
            finding(
                check_id="routes.no_financial_authority",
                passed=not financial,
                message="No route owns financial truth or downstream execution authority.",
                evidence={"financial_routes": financial},
                details={"financial_routes": financial},
            ),
            finding(
                check_id="routes.unsafe_requires_operator_auth",
                passed=not unsafe_without_auth,
                message="Every unsafe orchestration-control route requires operator authentication.",
                evidence={"unsafe_without_auth": unsafe_without_auth},
                details={"unsafe_without_auth": unsafe_without_auth},
            ),
            finding(
                check_id="deployment.loopback_only",
                passed=bind_ok,
                message="The operator deployment binds only to an approved loopback host.",
                evidence={"host_allowed": bind_ok},
                details={"host_allowed": bind_ok},
            ),
            finding(
                check_id="deployment.origins_exact",
                passed=origins_ok,
                message="Browser origins exactly match the local UI allowlist.",
                evidence={"origins": manifest.allowed_origins},
                details={"origins": manifest.allowed_origins if origins_ok else ()},
            ),
            finding(
                check_id="imports.no_live_broker_oauth_mandate",
                passed=not blocked_imports,
                message="The VeriQuant-only deployment imports no live, broker, OAuth, mandate, account, or order runtime.",
                evidence={"imported_modules": manifest.imported_modules},
                details={"blocked_imports": blocked_imports},
            ),
            finding(
                check_id="startup.offline_key_free",
                passed=manifest.offline_only and not manifest.provider_keys_required and not manifest.real_broker_enabled,
                message="The default app is offline, provider-key-free, and broker-free.",
                evidence={
                    "offline_only": manifest.offline_only,
                    "provider_keys_required": manifest.provider_keys_required,
                    "real_broker_enabled": manifest.real_broker_enabled,
                },
            ),
        )

    def authorize_request(
        self,
        *,
        method: str,
        path: str,
        host: str,
        origin: str | None,
        operator_authenticated: bool,
    ) -> tuple[SecurityFinding, ...]:
        normalized_method = str(method).strip().upper()
        host_ok = self._host_name(host) in set(self.allowed_hosts)
        origin_ok = origin is None or self._origin_allowed(origin)
        matched = self._match_route(normalized_method, path)
        route_ok = matched is not None
        auth_ok = route_ok and (normalized_method == "GET" or (bool(operator_authenticated) and origin is not None and origin_ok))
        return (
            finding(
                check_id="request.host_loopback",
                passed=host_ok,
                message="Host is an approved loopback name.",
                evidence={"host_allowed": host_ok},
                details={"host_allowed": host_ok},
            ),
            finding(
                check_id="request.origin_local",
                passed=origin_ok,
                message="Origin is absent for a non-browser client or is an approved local UI origin.",
                evidence={"origin_allowed": origin_ok},
                details={"origin_allowed": origin_ok},
            ),
            finding(
                check_id="request.route_authorized",
                passed=route_ok,
                message="Method and canonical path match the exact route allowlist.",
                evidence={"method": normalized_method, "path_hash": self._path_hash(path), "route_allowed": route_ok},
                details={"route_allowed": route_ok, "authority": matched.authority.value if matched else None},
            ),
            finding(
                check_id="request.unsafe_operator_authenticated",
                passed=bool(auth_ok),
                message="Unsafe methods require trusted operator authentication and an approved local Origin.",
                evidence={"method": normalized_method, "authenticated": bool(operator_authenticated), "auth_ok": bool(auth_ok)},
                details={"auth_ok": bool(auth_ok)},
            ),
        )

    def validate_create_payload(self, payload: Mapping[str, Any]) -> tuple[SecurityFinding, ...]:
        fields = {str(key) for key in payload}
        unknown = tuple(sorted(fields - ALLOWED_CREATE_FIELDS))
        missing = tuple(sorted(ALLOWED_CREATE_FIELDS - fields))
        forbidden = tuple(sorted(key for key in fields if self._contains_any(key, FORBIDDEN_INPUT_TOKENS)))
        types_ok = (
            not missing
            and isinstance(payload.get("profile_id"), str)
            and SAFE_ID.fullmatch(str(payload.get("profile_id"))) is not None
            and type(payload.get("seed")) is int
            and type(payload.get("budget")) is int
            and 1 <= int(payload.get("budget", 0)) <= 100
            and isinstance(payload.get("idempotency_key"), str)
            and SAFE_IDEMPOTENCY_KEY.fullmatch(str(payload.get("idempotency_key"))) is not None
        )
        values = tuple(str(value) for value in payload.values() if isinstance(value, str))
        executable_or_locator = any(
            self._contains_any(value, ("http://", "https://", "file://", "../", "..\\", ".py", "import ", "eval(", "exec(", "powershell", "cmd.exe"))
            for value in values
        )
        passed = not unknown and not missing and not forbidden and types_ok and not executable_or_locator
        return (
            finding(
                check_id="payload.closed_schema",
                passed=passed,
                message="Job creation accepts only a fixed profile, seed, bounded budget, and idempotency key.",
                evidence={"fields": tuple(sorted(fields)), "types_ok": types_ok, "locator_or_code": executable_or_locator},
                details={"unknown": unknown, "missing": missing, "forbidden": forbidden, "types_ok": types_ok},
            ),
        )

    def _match_route(self, method: str, path: str) -> RouteRecord | None:
        if not self._canonical_path(path):
            return None
        for route in self.expected_routes:
            if route.method != method:
                continue
            pattern = re.escape(route.path)
            pattern = pattern.replace(re.escape("{job_id}"), JOB_ID_SEGMENT)
            pattern = pattern.replace(re.escape("{run_id}"), JOB_ID_SEGMENT)
            if re.fullmatch(pattern, path):
                return route
        return None

    @staticmethod
    def _canonical_path(path: str) -> bool:
        if not isinstance(path, str) or not path.startswith("/"):
            return False
        if any(token in path for token in ("?", "#", "\\", "%", "//", "\x00")):
            return False
        return not any(segment in {".", ".."} for segment in path.split("/"))

    def _origin_allowed(self, origin: str) -> bool:
        try:
            parsed = urlsplit(str(origin))
        except ValueError:
            return False
        if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return False
        normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        return normalized in {item.lower() for item in self.allowed_origins}

    @staticmethod
    def _host_name(host: str) -> str:
        value = str(host).strip().lower()
        if value.startswith("[") and "]" in value:
            return value[1:value.index("]")]
        if value.count(":") == 1:
            return value.split(":", 1)[0]
        return value

    @staticmethod
    def _contains_any(value: str, tokens: Iterable[str]) -> bool:
        lowered = str(value).lower()
        return any(str(token).lower() in lowered for token in tokens)

    @staticmethod
    def _path_hash(path: str) -> str:
        from .models import canonical_hash

        return canonical_hash({"path": str(path)})

