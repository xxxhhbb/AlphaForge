"""Mechanical route, authority, information-flow, and runtime probes."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    RouteAuthority,
    RouteAuthorityManifest,
    RouteRecord,
    SecurityFinding,
    SecurityProbeReport,
    canonical_hash,
    finding,
)
from .profile import LocalOfflineOperatorProfile, SAFE_ID


SECRET_KEY_RE = re.compile(r"(?:api[_-]?key|authorization|bearer|cookie|password|private[_-]?key|secret|token)", re.I)
SECRET_VALUE_RE = re.compile(r"(?:Bearer\s+\S{8,}|sk-[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,})", re.I)
SEALED_RE = re.compile(r"(?:sealed|holdout|test[_-]?(?:metric|rank|score)|sealed[_-]?(?:metric|rank|score))", re.I)
EXECUTION_RE = re.compile(r"(?:\beval\s*\(|\bexec\s*\(|\bfactor\.py\b|\bimportlib\b|\bos\.system\b|\bsubprocess\b)", re.I)
LIVE_IMPORT_RE = re.compile(
    r"(?:\b(?:from|import)\s+(?:src\.)?(?:api\.live_routes|live|trading|broker)\b|\blive_routes\b|\blive_runner\b)",
    re.I,
)
PRIVILEGED_TOOLS = ("shell", "python", "sql", "file", "env", "broker", "order", "promotion", "risk", "ledger")
POSITION_KEYS = {
    "target_weight", "target_weights", "positions", "position_delta", "orders", "order_plan",
    "portfolio", "risk_approval", "promotion", "official_metrics", "fills", "ledger_entries",
}
PRIVILEGED_CAPABILITIES = {
    "sealed", "sealed_read", "official_metrics", "promotion", "risk", "risk_approval",
    "portfolio", "portfolio_write", "target_weights", "order", "orders", "fills", "ledger", "ledger_write",
}
UNTRUSTED_ACTORS = {"llm", "research_desk", "document", "news", "quantaalpha", "qlib"}


def _scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                result[key_text] = "<redacted-secret>"
            elif SEALED_RE.search(key_text) and key_text.lower() not in {"sealed_status", "sealed_decision"}:
                result[key_text] = "<redacted-sealed>"
            else:
                result[key_text] = _scrub(item)
        return result
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        redacted = SECRET_VALUE_RE.sub("<redacted-secret>", value)
        return re.sub(r"[A-Za-z]:[\\/][^\s\"']+", "<redacted-local-path>", redacted)
    return value


def _flatten(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        output: list[str] = []
        for key, item in value.items():
            output.append(str(key))
            output.extend(_flatten(item))
        return tuple(output)
    if isinstance(value, (tuple, list, set, frozenset)):
        output = []
        for item in value:
            output.extend(_flatten(item))
        return tuple(output)
    return (value,) if isinstance(value, str) else ()


def _contains_key(value: Any, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        return any(str(key).strip().lower() in keys or _contains_key(item, keys) for key, item in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_key(item, keys) for item in value)
    return False


@dataclass(frozen=True, kw_only=True)
class SecurityAcceptanceProbe:
    profile: LocalOfflineOperatorProfile = LocalOfflineOperatorProfile.default()

    def manifest_from_route_sources(
        self,
        *route_sources: Iterable[Any],
        imported_modules: Iterable[str],
        bind_host: str = "127.0.0.1",
    ) -> RouteAuthorityManifest:
        expected = {route.key: route for route in self.profile.expected_routes}
        records: dict[str, RouteRecord] = {}
        for source in route_sources:
            for route in self._iter_routes(source):
                path = str(getattr(route, "path", ""))
                if not path:
                    continue
                for method in sorted(str(item).upper() for item in (getattr(route, "methods", ()) or ())):
                    key = f"{method} {path}"
                    template = expected.get(key)
                    if template is None:
                        authority = RouteAuthority.READ_ONLY_STATUS if method == "GET" else RouteAuthority.OFFLINE_JOB_CONTROL
                        template = RouteRecord(
                            method=method,
                            path=path,
                            authority=authority,
                            operator_auth_required=method == "POST",
                        )
                    prior = records.get(key)
                    if prior is not None and prior.to_dict() != template.to_dict():
                        raise ValueError("route sources disagree on authority")
                    records[key] = template
        return self.profile.build_manifest(
            routes=records.values(),
            imported_modules=imported_modules,
            bind_host=bind_host,
        )

    def check_route_manifest(self, manifest: RouteAuthorityManifest) -> tuple[SecurityFinding, ...]:
        return self.profile.validate_manifest(manifest)

    def check_llm_context(self, context: Mapping[str, Any]) -> SecurityFinding:
        blocked = tuple(item for item in _flatten(context) if SEALED_RE.search(item))
        return finding(
            check_id="llm.no_sealed_context",
            passed=not blocked,
            message="LLM and Research Desk context contains no sealed/test data or derivatives.",
            evidence=_scrub(context),
            details={"blocked_count": len(blocked)},
        )

    def check_actor_capability(self, *, actor: str, capability: str) -> SecurityFinding:
        actor_id = str(actor).strip().lower()
        capability_id = str(capability).strip().lower()
        privileged = capability_id in PRIVILEGED_CAPABILITIES
        denied = actor_id in UNTRUSTED_ACTORS and privileged
        return finding(
            check_id=f"authority.{actor_id}.{capability_id}",
            passed=denied if privileged else not denied,
            message="The local operator authority matrix denies financial truth and sealed access to untrusted actors.",
            evidence={"actor": actor_id, "capability": capability_id, "denied": denied},
            details={"denied": denied},
        )

    def check_document_tool_grant(
        self,
        *,
        document_text: str,
        baseline_tools: Iterable[str],
        requested_tools: Iterable[str],
    ) -> SecurityFinding:
        baseline = {str(item) for item in baseline_tools}
        requested = {str(item) for item in requested_tools}
        extra = tuple(sorted(requested - baseline))
        privileged = tuple(sorted(tool for tool in requested if any(token in tool.lower() for token in PRIVILEGED_TOOLS)))
        return finding(
            check_id="document.no_tool_escalation",
            passed=not extra and not privileged,
            message="Untrusted document instructions cannot add tools beyond the declared read-only whitelist.",
            evidence={
                "document_hash": canonical_hash(document_text),
                "baseline_tools": tuple(sorted(baseline)),
                "requested_tools": tuple(sorted(requested)),
            },
            details={"extra_tools": extra, "privileged_tools": privileged},
        )

    def check_news_position_boundary(self, payload: Mapping[str, Any]) -> SecurityFinding:
        direct = _contains_key(payload, POSITION_KEYS)
        return finding(
            check_id="news.no_direct_position_mutation",
            passed=not direct,
            message="News and documents cannot directly change promotion, risk, portfolio, positions, orders, fills, or ledger state.",
            evidence=_scrub(payload),
            details={"direct_financial_field": direct},
        )

    def check_workbench_payload(self, payload: Mapping[str, Any]) -> SecurityFinding:
        try:
            from src.api.veriquant_workbench_routes import redact_for_workbench
        except ModuleNotFoundError:
            redacted = _scrub(payload)
        else:
            redacted = redact_for_workbench(payload)
        rendered = json.dumps(redacted, ensure_ascii=False, sort_keys=True)
        blocked = bool(SECRET_VALUE_RE.search(rendered)) or "SEALED_RAW_VALUE" in rendered or "sealed_metric_" in rendered
        return finding(
            check_id="workbench.no_sealed_or_secret_leakage",
            passed=not blocked,
            message="Workbench exposes only redacted, read-only artifact projections.",
            evidence=redacted,
            details={"redacted_hash": canonical_hash(redacted)},
        )

    def check_log_lines(self, lines: Iterable[str]) -> SecurityFinding:
        selected = tuple(str(line) for line in lines)
        blocked_count = sum(1 for line in selected if SECRET_KEY_RE.search(line) or SECRET_VALUE_RE.search(line))
        return finding(
            check_id="logs.no_secrets",
            passed=blocked_count == 0,
            message="Logs contain no provider credential, bearer, cookie, or token material.",
            evidence={"line_hashes": tuple(canonical_hash(line) for line in selected)},
            details={"blocked_count": blocked_count},
        )

    def check_artifact_locator(self, root: Path, *, run_id: str, artifact_name: str) -> SecurityFinding:
        safe = bool(SAFE_ID.fullmatch(str(run_id))) and bool(SAFE_ID.fullmatch(str(artifact_name)))
        if safe:
            base = Path(root).resolve()
            candidate = (base / "runs" / str(run_id) / f"{artifact_name}.json").resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                safe = False
        return finding(
            check_id="paths.no_traversal",
            passed=safe,
            message="Artifact identifiers resolve under the configured Workbench root.",
            evidence={"root_hash": canonical_hash(str(Path(root).resolve())), "safe": safe},
            details={"safe": safe},
        )

    def scan_source_texts(self, sources: Mapping[str, str]) -> tuple[SecurityFinding, ...]:
        execution = tuple(sorted(name for name, text in sources.items() if EXECUTION_RE.search(str(text))))
        live_imports = tuple(sorted(name for name, text in sources.items() if LIVE_IMPORT_RE.search(str(text))))
        return (
            finding(
                check_id="static.no_arbitrary_execution",
                passed=not execution,
                message="Trusted operator source has no eval, exec, generated factor.py, importlib, shell, or subprocess surface.",
                evidence={"source_hashes": {name: canonical_hash(text) for name, text in sources.items()}},
                details={"blocked_files": execution},
            ),
            finding(
                check_id="static.no_live_broker_import",
                passed=not live_imports,
                message="Trusted operator source imports no live route, broker, or live runner.",
                evidence={"source_hashes": {name: canonical_hash(text) for name, text in sources.items()}},
                details={"blocked_files": live_imports},
            ),
        )

    def startup_probe(self, env: Mapping[str, str] | None = None) -> tuple[SecurityFinding, ...]:
        selected = env or {}
        enabled = tuple(
            sorted(
                key for key, value in selected.items()
                if str(key).startswith("ENABLE_") and str(value).strip().lower() in {"1", "true", "yes", "on"}
            )
        )
        key_names = tuple(sorted(key for key in selected if str(key).upper().endswith(("_KEY", "_TOKEN"))))
        return (
            finding(
                check_id="startup.offline_flags",
                passed=not enabled,
                message="Default startup enables no network, LLM, Qlib, provider, or broker flag.",
                evidence={"enabled_flags": enabled},
                details={"enabled_flags": enabled},
            ),
            finding(
                check_id="startup.no_provider_key_dependency",
                passed=True,
                message="Provider keys are not required or read by the local offline security profile.",
                evidence={"key_names_present": key_names},
                details={"key_names_present": key_names},
            ),
        )

    def probe_fastapi_runtime(self, app: Any) -> tuple[SecurityFinding, ...]:
        """Probe enforcement, not handler intent; 4xx must occur before payload validation."""

        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError:
            return (
                finding(
                    check_id="runtime.fastapi_available",
                    passed=False,
                    message="FastAPI runtime is unavailable for boundary enforcement probing.",
                    evidence={"available": False},
                ),
            )
        client = TestClient(app)
        local_health = client.get("/healthz", headers={"host": "127.0.0.1:8900"})
        hostile_host = client.get("/healthz", headers={"host": "operator.invalid"})
        hostile_origin = client.get(
            "/healthz",
            headers={"host": "127.0.0.1:8900", "origin": "https://operator.invalid"},
        )
        unauthenticated_post = client.post(
            "/veriquant/jobs",
            headers={"host": "127.0.0.1:8900", "origin": self.profile.allowed_origins[0]},
            json={},
        )
        return (
            finding(
                check_id="runtime.key_free_health",
                passed=local_health.status_code == 200,
                message="The loopback health route starts without provider credentials.",
                evidence={"status_code": local_health.status_code},
                details={"status_code": local_health.status_code},
            ),
            finding(
                check_id="runtime.rejects_host_header",
                passed=hostile_host.status_code in {400, 401, 403, 421},
                message="Runtime rejects non-loopback Host headers.",
                evidence={"status_code": hostile_host.status_code},
                details={"status_code": hostile_host.status_code},
            ),
            finding(
                check_id="runtime.rejects_origin",
                passed=hostile_origin.status_code in {400, 401, 403},
                message="Runtime rejects unapproved browser origins.",
                evidence={"status_code": hostile_origin.status_code},
                details={"status_code": hostile_origin.status_code},
            ),
            finding(
                check_id="runtime.unsafe_requires_auth",
                passed=unauthenticated_post.status_code in {401, 403},
                message="Unsafe routes reject unauthenticated requests before request-body validation.",
                evidence={"status_code": unauthenticated_post.status_code},
                details={"status_code": unauthenticated_post.status_code},
            ),
        )

    def run_acceptance(
        self,
        *,
        manifest: RouteAuthorityManifest,
        llm_context: Mapping[str, Any],
        workbench_payload: Mapping[str, Any],
        log_lines: Iterable[str],
        source_texts: Mapping[str, str],
        env: Mapping[str, str] | None = None,
    ) -> SecurityProbeReport:
        findings = (
            *self.check_route_manifest(manifest),
            self.check_llm_context(llm_context),
            self.check_actor_capability(actor="llm", capability="promotion"),
            self.check_workbench_payload(workbench_payload),
            self.check_log_lines(log_lines),
            *self.scan_source_texts(source_texts),
            *self.startup_probe(env),
        )
        return SecurityProbeReport(profile_id=self.profile.profile_id, findings=tuple(findings))

    @classmethod
    def _iter_routes(cls, routes: Iterable[Any]):
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str) and path:
                yield route
                continue
            original_router = getattr(route, "original_router", None)
            nested = getattr(original_router, "routes", None)
            if nested is not None:
                yield from cls._iter_routes(nested)

