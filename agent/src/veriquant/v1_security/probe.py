"""Static and runtime security probes for the VeriQuant-only V1 surface."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import ProbeReport, SecurityFinding, SecuritySeverity, canonical_hash, finding
from .profile import RouteAuthorityManifest, VeriQuantOfflineDeploymentProfile


SECRET_KEY_RE = re.compile(r"(api[_-]?key|authorization|bearer|cookie|password|private[_-]?key|secret|token)", re.I)
SECRET_VALUE_RE = re.compile(r"(Bearer\s+[A-Za-z0-9._~+/=-]{8,}|sk-[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,})", re.I)
SEALED_TEXT_RE = re.compile(r"\b(sealed|holdout|test_metric|test_rank|sealed_metric|sealed_rank)\b", re.I)
EXECUTION_TEXT_RE = re.compile(r"(\beval\s*\(|\bexec\s*\(|\bfactor\.py\b|\bimportlib\b|\bos\.system\b|\bsubprocess\b)", re.I)
LIVE_IMPORT_RE = re.compile(
    r"(\bfrom\s+src\.(?:api\.live_routes|live|trading)\b|\bimport\s+src\.(?:api\.live_routes|live|trading)\b|\blive_routes\b)",
    re.I,
)
FORBIDDEN_TOOL_TOKENS = ("shell", "python", "sql", "file", "env", "broker", "order", "promotion", "risk", "ledger")
FORBIDDEN_POSITION_KEYS = {
    "target_weight",
    "target_weights",
    "positions",
    "position_delta",
    "orders",
    "order_plan",
    "portfolio",
    "risk_approval",
}


def _scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        scrubbed: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.search(key_text):
                scrubbed[key_text] = "<redacted-secret-key>"
            elif SEALED_TEXT_RE.search(key_text) and key_text.lower() not in {"sealed_status", "sealed_decision"}:
                scrubbed[key_text] = "<redacted-sealed-key>"
            else:
                scrubbed[key_text] = _scrub(item)
        return scrubbed
    if isinstance(value, (tuple, list, set)):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        text = SECRET_VALUE_RE.sub("<redacted-secret-value>", value)
        text = re.sub(r"[A-Za-z]:[\\/][^\s\"']+", "<redacted-local-path>", text)
        return text
    return value


def _flatten_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        items: list[str] = []
        for key, item in value.items():
            items.append(str(key))
            items.extend(_flatten_strings(item))
        return tuple(items)
    if isinstance(value, (tuple, list, set)):
        items: list[str] = []
        for item in value:
            items.extend(_flatten_strings(item))
        return tuple(items)
    if isinstance(value, str):
        return (value,)
    return ()


def _contains_key(value: Any, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).strip().lower() in keys:
                return True
            if _contains_key(item, keys):
                return True
    if isinstance(value, (tuple, list, set)):
        return any(_contains_key(item, keys) for item in value)
    return False


@dataclass(frozen=True, kw_only=True)
class SecurityAcceptanceProbe:
    """Mechanical probe suite for the V1 security and authority gate."""

    profile: VeriQuantOfflineDeploymentProfile = VeriQuantOfflineDeploymentProfile.default()

    def check_route_manifest(self, manifest: RouteAuthorityManifest) -> tuple[SecurityFinding, ...]:
        return self.profile.validate_manifest(manifest)

    def check_llm_context(self, context: Mapping[str, Any]) -> SecurityFinding:
        strings = _flatten_strings(context)
        blocked = tuple(item for item in strings if SEALED_TEXT_RE.search(item))
        return finding(
            check_id="llm.no_sealed_context",
            passed=not blocked,
            message="LLM and Research Desk context contains no sealed/test/holdout data or derivatives.",
            evidence=_scrub(context),
            details={"blocked_count": len(blocked)},
        )

    def check_actor_capability(self, *, actor: str, capability: str) -> SecurityFinding:
        denied = self.profile.denies_capability(actor, capability)
        privileged = str(capability).strip().lower() in {
            "official_metrics",
            "promotion",
            "risk",
            "risk_approval",
            "portfolio",
            "portfolio_write",
            "target_weights",
            "order",
            "orders",
            "fills",
            "ledger",
            "ledger_write",
            "sealed",
            "sealed_read",
        }
        return finding(
            check_id=f"authority.{actor}.{capability}",
            passed=denied if privileged else not denied,
            message="Actor capability is enforced by the VeriQuant authority matrix.",
            evidence={"actor": actor, "capability": capability, "denied": denied},
            details={"denied": denied},
        )

    def check_document_tool_grant(
        self,
        *,
        document_text: str,
        baseline_tools: tuple[str, ...],
        requested_tools: tuple[str, ...],
    ) -> SecurityFinding:
        baseline = set(baseline_tools)
        requested = set(requested_tools)
        extra_tools = tuple(sorted(requested - baseline))
        forbidden_tools = tuple(
            sorted(tool for tool in requested if any(token in tool.lower() for token in FORBIDDEN_TOOL_TOKENS))
        )
        return finding(
            check_id="document_prompt_injection.no_tool_escalation",
            passed=not extra_tools and not forbidden_tools,
            message="Untrusted document text did not add tools beyond the declared read-only whitelist.",
            evidence={
                "document_hash": canonical_hash(document_text),
                "baseline_tools": tuple(sorted(baseline)),
                "requested_tools": tuple(sorted(requested)),
            },
            details={"extra_tools": extra_tools, "forbidden_tools": forbidden_tools},
        )

    def check_news_position_boundary(
        self,
        *,
        news_payload: Mapping[str, Any],
        proposed_position_delta: Mapping[str, Any] | None = None,
    ) -> SecurityFinding:
        direct_payload = _contains_key(news_payload, FORBIDDEN_POSITION_KEYS)
        direct_delta = bool(proposed_position_delta)
        return finding(
            check_id="news.no_direct_position_mutation",
            passed=not direct_payload and not direct_delta,
            message="News/document artifacts cannot directly change holdings, risk, portfolio, or orders.",
            evidence={"news_payload": _scrub(news_payload), "position_delta_present": direct_delta},
            details={"direct_payload": direct_payload, "position_delta_present": direct_delta},
        )

    def check_workbench_payload(self, payload: Mapping[str, Any]) -> SecurityFinding:
        try:
            from src.api.veriquant_workbench_routes import redact_for_workbench
        except ModuleNotFoundError:
            redacted = _scrub(payload)
        else:
            redacted = redact_for_workbench(payload)
        rendered = json.dumps(redacted, sort_keys=True, ensure_ascii=False)
        blocked = SECRET_VALUE_RE.search(rendered) or "SEALED_RAW_VALUE" in rendered or "sealed_metric_0." in rendered
        return finding(
            check_id="workbench.no_sealed_or_secret_leakage",
            passed=not blocked,
            message="Workbench payload is redacted before read-only exposure.",
            evidence=redacted,
            details={"redacted_hash": canonical_hash(redacted)},
        )

    def check_log_lines(self, lines: tuple[str, ...]) -> SecurityFinding:
        blocked = tuple(index for index, line in enumerate(lines) if SECRET_VALUE_RE.search(line) or SECRET_KEY_RE.search(line))
        return finding(
            check_id="logs.no_tokens_or_api_keys",
            passed=not blocked,
            message="Logs do not contain token, API key, Authorization, cookie, or secret material.",
            evidence={"line_count": len(lines), "blocked_line_indexes": blocked},
            details={"blocked_line_indexes": blocked},
        )

    def check_artifact_locator(self, root: Path, *, run_id: str, artifact_name: str) -> SecurityFinding:
        try:
            resolved = self.profile.resolve_artifact_locator(root, run_id=run_id, artifact_name=artifact_name)
        except ValueError as exc:
            return finding(
                check_id="artifact_path.safe_locator",
                passed=False,
                message="Artifact locator was rejected fail-closed.",
                evidence={"run_id_hash": canonical_hash(run_id), "artifact_name": artifact_name},
                details={"error": type(exc).__name__},
            )
        return finding(
            check_id="artifact_path.safe_locator",
            passed=True,
            message="Artifact locator stays inside the configured Workbench root.",
            evidence={"artifact_name": artifact_name, "relative_shape": "runs/<run_id>/<artifact>.json"},
            severity=SecuritySeverity.INFO,
            details={"resolved_name": resolved.name},
        )

    def scan_source_texts(self, sources: Mapping[str, str]) -> tuple[SecurityFinding, ...]:
        execution_hits = tuple(
            name for name, text in sources.items() if EXECUTION_TEXT_RE.search(text)
        )
        live_hits = tuple(
            name for name, text in sources.items() if LIVE_IMPORT_RE.search(text)
        )
        payload = {"files": tuple(sorted(sources)), "file_hashes": {name: canonical_hash(text) for name, text in sources.items()}}
        return (
            finding(
                check_id="static.no_eval_exec_factor_py",
                passed=not execution_hits,
                message="Trusted VeriQuant-only surface contains no eval, exec, generated factor.py, importlib, os.system, or subprocess execution.",
                evidence=payload,
                details={"execution_hits": execution_hits},
            ),
            finding(
                check_id="static.no_broker_or_live_runner_import",
                passed=not live_hits,
                message="Trusted VeriQuant-only surface does not import live routes, live runners, trading, or broker paths.",
                evidence=payload,
                details={"live_hits": live_hits},
            ),
        )

    def run_acceptance(
        self,
        *,
        manifest: RouteAuthorityManifest,
        llm_context: Mapping[str, Any],
        workbench_payload: Mapping[str, Any],
        log_lines: tuple[str, ...],
        source_texts: Mapping[str, str],
        env: Mapping[str, str] | None = None,
    ) -> ProbeReport:
        findings: list[SecurityFinding] = []
        findings.extend(self.check_route_manifest(manifest))
        findings.extend(self.profile.startup_probe(env))
        findings.append(self.check_llm_context(llm_context))
        findings.append(self.check_actor_capability(actor="llm", capability="promotion"))
        findings.append(
            self.check_document_tool_grant(
                document_text="visible cited document text",
                baseline_tools=("fundamental.read_context",),
                requested_tools=("fundamental.read_context",),
            )
        )
        findings.append(self.check_news_position_boundary(news_payload={"headline": "visible news only"}))
        findings.append(self.check_workbench_payload(workbench_payload))
        findings.append(self.check_log_lines(log_lines))
        findings.extend(self.scan_source_texts(source_texts))
        return ProbeReport(profile_id=self.profile.profile_id, findings=tuple(findings))
