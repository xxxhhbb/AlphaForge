"""Mechanical reconciliation of stale and current V1 acceptance evidence.

The reconciler is deliberately conservative:

* it reads existing artifacts only;
* it never marks global V1 as passed;
* it fails closed when critical artifacts, hashes, or source labels are absent;
* it treats the C8 approved-proxy run as current evidence without silently
  deleting older failures from final/UI acceptance artifacts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


IMPLEMENTED_AND_TESTED = "IMPLEMENTED_AND_TESTED"
IMPLEMENTED_NOT_RUN = "IMPLEMENTED_NOT_RUN"
UNAVAILABLE = "UNAVAILABLE"
FAILED = "FAILED"

READY_FOR_FINAL_ACCEPTANCE = "READY_FOR_FINAL_ACCEPTANCE"
NEEDS_FINAL_ACCEPTANCE_REBASE = "NEEDS_FINAL_ACCEPTANCE_REBASE"
BLOCKED = "BLOCKED"
FAILED_CLOSED = "FAILED_CLOSED"

APPROVED_PROXY_DATA_VENDOR = "APPROVED_PROXY_DATA_VENDOR"
TUSHARE_COMPATIBLE_PROXY = "TUSHARE_COMPATIBLE_PROXY"
OFFICIAL_TUSHARE = "OFFICIAL_TUSHARE"


class AcceptanceReconcileFailureCode(str, Enum):
    """Typed failure codes for fail-closed reconciliation."""

    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    INVALID_JSON = "INVALID_JSON"
    MISSING_ARTIFACT_HASH = "MISSING_ARTIFACT_HASH"
    HASH_MISMATCH = "HASH_MISMATCH"
    MISSING_C8_RESULT = "MISSING_C8_RESULT"
    C8_NOT_PASSED = "C8_NOT_PASSED"
    SYNTHETIC_NOT_REAL = "SYNTHETIC_NOT_REAL"
    FORBIDDEN_OFFICIAL_TUSHARE = "FORBIDDEN_OFFICIAL_TUSHARE"
    MISSING_GATE_EVIDENCE = "MISSING_GATE_EVIDENCE"
    STALE_ACCEPTANCE = "STALE_ACCEPTANCE"


@dataclass(frozen=True)
class AcceptanceConflict:
    """A difference between stale acceptance artifacts and current evidence."""

    conflict_id: str
    artifact: str
    gate: str
    stale: bool
    previous_status: str
    current_status: str
    reason: str


@dataclass(frozen=True)
class AcceptanceEvidenceSnapshot:
    """Stable read model of the artifacts considered by the reconciler."""

    schema_version: str
    state_path: str
    final_acceptance_path: str
    ui_acceptance_path: str
    c8_result_path: str
    c8_report_path: str
    state_hash: str | None
    final_acceptance_hash: str | None
    ui_acceptance_hash: str | None
    c8_result_hash: str | None
    c8_report_hash: str | None
    final_acceptance_status: str | None
    ui_acceptance_status: str | None
    c8_status: str | None
    c8_data_source_class: str | None
    c8_source_class: str | None
    c8_official_tushare_source: bool | None
    c8_snapshot_hash: str | None
    c8_calendar_hash: str | None
    c8_universe_hash: str | None
    c8_shadow_session_count: int | None
    c8_daily_vintage_count: int | None
    parallel_package_count: int
    parallel_ready_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcceptanceReconcileVerdict:
    """Final read-only reconciliation verdict."""

    schema_version: str
    verdict: str
    gates: dict[str, str]
    snapshot: AcceptanceEvidenceSnapshot
    conflicts: tuple[AcceptanceConflict, ...] = ()
    blockers: tuple[dict[str, str], ...] = ()
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    report_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["report_hash"] = self.report_hash or stable_hash(
            _without_keys(data, {"report_hash"})
        )
        return data


class AcceptanceReconciler:
    """Read existing V1 artifacts and reconcile stale failures against C8 facts."""

    STATE_PATH = ".autobuild/veriquant/state.json"
    FINAL_ACCEPTANCE_PATH = ".autobuild/veriquant/integration/final_acceptance.json"
    UI_ACCEPTANCE_PATH = (
        ".autobuild/veriquant/integration/ui_acceptance/acceptance_verdict.json"
    )
    C8_RESULT_PATH = (
        ".autobuild/veriquant/integration/c8_real_ashare_v1/"
        "c8-real-ashare-v1/result.json"
    )
    C8_REPORT_PATH = (
        ".autobuild/veriquant/integration/c8_real_ashare_v1/"
        "c8-real-ashare-v1/report.md"
    )

    CRITICAL_HASH_PATHS = (
        FINAL_ACCEPTANCE_PATH,
        UI_ACCEPTANCE_PATH,
        C8_RESULT_PATH,
        C8_REPORT_PATH,
    )

    def __init__(self, repo_root: str | Path) -> None:
        self.repo_root = Path(repo_root)

    def reconcile(self) -> AcceptanceReconcileVerdict:
        blockers: list[dict[str, str]] = []
        artifact_hashes: dict[str, str] = {}

        state = self._load_json(self.STATE_PATH, blockers, required=True)
        final_acceptance = self._load_json(
            self.FINAL_ACCEPTANCE_PATH, blockers, required=True
        )
        ui_acceptance = self._load_json(self.UI_ACCEPTANCE_PATH, blockers, required=True)
        c8_result = self._load_json(self.C8_RESULT_PATH, blockers, required=True)
        if c8_result is None:
            blockers.append(
                _blocker(
                    AcceptanceReconcileFailureCode.MISSING_C8_RESULT,
                    self.C8_RESULT_PATH,
                    "C8 result is required to reconcile superseded V1 failures.",
                )
            )
        c8_report_text = self._load_text(self.C8_REPORT_PATH, blockers, required=True)

        for rel_path in (
            self.STATE_PATH,
            self.FINAL_ACCEPTANCE_PATH,
            self.UI_ACCEPTANCE_PATH,
            self.C8_RESULT_PATH,
            self.C8_REPORT_PATH,
        ):
            file_hash = self._file_hash(rel_path)
            if file_hash:
                artifact_hashes[rel_path] = file_hash

        self._validate_recorded_hashes(state, artifact_hashes, blockers)

        parallel_ready = tuple(self._parallel_ready_packages())
        snapshot = self._build_snapshot(
            artifact_hashes=artifact_hashes,
            final_acceptance=final_acceptance,
            ui_acceptance=ui_acceptance,
            c8_result=c8_result,
            parallel_ready=parallel_ready,
        )

        c8_passed = (snapshot.c8_status or "").upper() == "PASSED"
        if c8_result is not None and not c8_passed:
            blockers.append(
                _blocker(
                    AcceptanceReconcileFailureCode.C8_NOT_PASSED,
                    self.C8_RESULT_PATH,
                    f"C8 status is {snapshot.c8_status!r}, not PASSED.",
                )
            )

        forbidden_source_label = self._has_forbidden_official_source(c8_result)
        if forbidden_source_label:
            blockers.append(
                _blocker(
                    AcceptanceReconcileFailureCode.FORBIDDEN_OFFICIAL_TUSHARE,
                    self.C8_RESULT_PATH,
                    "Approved proxy evidence is incorrectly labeled OFFICIAL_TUSHARE.",
                )
            )

        gates, gate_blockers = self._current_gates(state, c8_result, c8_passed)
        blockers.extend(gate_blockers)
        conflicts = self._conflicts(final_acceptance, ui_acceptance, gates)

        if blockers:
            verdict = FAILED_CLOSED
        elif all(status == IMPLEMENTED_AND_TESTED for status in gates.values()):
            stale_failure_exists = any(conflict.stale for conflict in conflicts)
            verdict = (
                NEEDS_FINAL_ACCEPTANCE_REBASE
                if stale_failure_exists
                else READY_FOR_FINAL_ACCEPTANCE
            )
        else:
            verdict = BLOCKED

        verdict_obj = AcceptanceReconcileVerdict(
            schema_version="veriquant.acceptance_reconcile/v1",
            verdict=verdict,
            gates=gates,
            snapshot=snapshot,
            conflicts=tuple(conflicts),
            blockers=tuple(blockers),
            artifact_hashes=artifact_hashes,
        )
        return AcceptanceReconcileVerdict(
            schema_version=verdict_obj.schema_version,
            verdict=verdict_obj.verdict,
            gates=verdict_obj.gates,
            snapshot=verdict_obj.snapshot,
            conflicts=verdict_obj.conflicts,
            blockers=verdict_obj.blockers,
            artifact_hashes=verdict_obj.artifact_hashes,
            report_hash=stable_hash(_without_keys(verdict_obj.to_dict(), {"report_hash"})),
        )

    def write_outputs(
        self,
        *,
        json_path: str | Path | None = None,
        markdown_path: str | Path | None = None,
    ) -> AcceptanceReconcileVerdict:
        verdict = self.reconcile()
        json_target = Path(json_path) if json_path else self.repo_root / (
            ".autobuild/veriquant/parallel/acceptance_reconcile/"
            "reconciliation.json"
        )
        md_target = Path(markdown_path) if markdown_path else self.repo_root / (
            "docs/veriquant/parallel/ACCEPTANCE_RECONCILE.md"
        )

        json_target.parent.mkdir(parents=True, exist_ok=True)
        md_target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            json_target,
            json.dumps(verdict.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
        )
        _atomic_write_text(md_target, render_markdown(verdict))
        return verdict

    def _build_snapshot(
        self,
        *,
        artifact_hashes: dict[str, str],
        final_acceptance: dict[str, Any] | None,
        ui_acceptance: dict[str, Any] | None,
        c8_result: dict[str, Any] | None,
        parallel_ready: tuple[str, ...],
    ) -> AcceptanceEvidenceSnapshot:
        c8_result = c8_result or {}
        snapshot = c8_result.get("snapshot") or {}
        shadow_replay = c8_result.get("shadow_replay") or {}
        return AcceptanceEvidenceSnapshot(
            schema_version="veriquant.acceptance_reconcile.snapshot/v1",
            state_path=self.STATE_PATH,
            final_acceptance_path=self.FINAL_ACCEPTANCE_PATH,
            ui_acceptance_path=self.UI_ACCEPTANCE_PATH,
            c8_result_path=self.C8_RESULT_PATH,
            c8_report_path=self.C8_REPORT_PATH,
            state_hash=artifact_hashes.get(self.STATE_PATH),
            final_acceptance_hash=artifact_hashes.get(self.FINAL_ACCEPTANCE_PATH),
            ui_acceptance_hash=artifact_hashes.get(self.UI_ACCEPTANCE_PATH),
            c8_result_hash=artifact_hashes.get(self.C8_RESULT_PATH),
            c8_report_hash=artifact_hashes.get(self.C8_REPORT_PATH),
            final_acceptance_status=_status(final_acceptance),
            ui_acceptance_status=_status(ui_acceptance),
            c8_status=_status(c8_result),
            c8_data_source_class=_string_or_none(c8_result.get("data_source_class")),
            c8_source_class=_string_or_none(c8_result.get("source_class")),
            c8_official_tushare_source=_bool_or_none(
                c8_result.get("official_tushare_source")
            ),
            c8_snapshot_hash=_string_or_none(snapshot.get("snapshot_hash")),
            c8_calendar_hash=_string_or_none(snapshot.get("calendar_hash")),
            c8_universe_hash=_string_or_none(snapshot.get("universe_hash")),
            c8_shadow_session_count=_int_or_none(shadow_replay.get("sessions_committed")),
            c8_daily_vintage_count=_int_or_none(shadow_replay.get("daily_vintage_count")),
            parallel_package_count=len(tuple(self._parallel_result_files())),
            parallel_ready_packages=parallel_ready,
        )

    def _current_gates(
        self,
        state: dict[str, Any] | None,
        c8_result: dict[str, Any] | None,
        c8_passed: bool,
    ) -> tuple[dict[str, str], list[dict[str, str]]]:
        blockers: list[dict[str, str]] = []
        scope_guard = (state or {}).get("scope_guard") or {}
        c8_result = c8_result or {}
        snapshot = c8_result.get("snapshot") or {}
        promotion = c8_result.get("promotion") or {}
        shadow = c8_result.get("shadow_replay") or {}
        security = c8_result.get("security") or {}
        invariants = c8_result.get("invariants") or {}

        gates = {
            "offline_end_to_end": IMPLEMENTED_AND_TESTED
            if _state_has_successful_test(state, "offline")
            else IMPLEMENTED_NOT_RUN,
            "approved_proxy_real_ashare_smoke": UNAVAILABLE,
            "twenty_session_shadow_replay": UNAVAILABLE,
            "security_authority": UNAVAILABLE,
            "recoverability_reproducibility": UNAVAILABLE,
        }

        real_smoke_ok = all(
            [
                c8_passed,
                c8_result.get("data_source_class") == APPROVED_PROXY_DATA_VENDOR,
                c8_result.get("source_class") == TUSHARE_COMPATIBLE_PROXY,
                c8_result.get("official_tushare_source") is False,
                scope_guard.get("approved_proxy_data_vendor") is True,
                scope_guard.get("official_tushare_source") is False,
                scope_guard.get("real_approved_proxy_snapshot_run") is True,
                scope_guard.get("real_approved_proxy_official_visible_evidence_run")
                is True,
                bool(snapshot.get("snapshot_hash")),
                bool(snapshot.get("calendar_hash")),
                bool(snapshot.get("universe_hash")),
                int(snapshot.get("row_count") or 0) > 0,
                bool(promotion.get("promotion_run"))
                or bool(scope_guard.get("real_statistical_promotion_run")),
            ]
        )
        if real_smoke_ok:
            gates["approved_proxy_real_ashare_smoke"] = IMPLEMENTED_AND_TESTED
        elif c8_passed:
            blockers.append(
                _blocker(
                    AcceptanceReconcileFailureCode.SYNTHETIC_NOT_REAL,
                    self.C8_RESULT_PATH,
                    "C8 passed but approved-proxy real A-share snapshot/provenance evidence is incomplete.",
                )
            )

        replay_ok = all(
            [
                c8_passed,
                scope_guard.get("twenty_session_shadow_replay") is True,
                _int_or_none(shadow.get("sessions_committed")) == 20,
                _int_or_none(shadow.get("daily_vintage_count")) == 20,
                shadow.get("forced_interruption") is True,
                shadow.get("resume_completed") is True,
                shadow.get("rerun_no_duplicate_journal") is True,
                bool(shadow.get("journal_tip_hash") or shadow.get("shadow_ledger_tip_hash")),
            ]
        )
        if replay_ok:
            gates["twenty_session_shadow_replay"] = IMPLEMENTED_AND_TESTED

        security_ok = all(
            [
                c8_passed,
                scope_guard.get("standalone_veriquant_api_no_live_routes") is True,
                scope_guard.get("veriquant_real_broker_enabled") is False,
                scope_guard.get("real_orders") is False,
                scope_guard.get("real_funds") is False,
                scope_guard.get("secrets_accessed") is False,
                security.get("status") == "PASSED",
                invariants.get("security_probe_passed") is True,
            ]
        )
        if security_ok:
            gates["security_authority"] = IMPLEMENTED_AND_TESTED

        recovery_ok = all(
            [
                c8_passed,
                scope_guard.get("midrun_resume_tested") is True,
                scope_guard.get("shadow_replay_rerun_no_duplicate_journal") is True,
                shadow.get("forced_interruption") is True,
                shadow.get("resume_completed") is True,
                shadow.get("rerun_no_duplicate_journal") is True,
                bool(self._file_hash(self.C8_RESULT_PATH)),
                bool(c8_result.get("artifact_hash")),
            ]
        )
        if recovery_ok:
            gates["recoverability_reproducibility"] = IMPLEMENTED_AND_TESTED

        for gate, status in gates.items():
            if status != IMPLEMENTED_AND_TESTED:
                blockers.append(
                    _blocker(
                        AcceptanceReconcileFailureCode.MISSING_GATE_EVIDENCE,
                        gate,
                        f"Gate {gate} has status {status}; critical V1 gate evidence is incomplete.",
                    )
                )
        return gates, blockers

    def _conflicts(
        self,
        final_acceptance: dict[str, Any] | None,
        ui_acceptance: dict[str, Any] | None,
        current_gates: dict[str, str],
    ) -> list[AcceptanceConflict]:
        conflicts: list[AcceptanceConflict] = []
        final_gates = (final_acceptance or {}).get("v1_gates") or {}
        ui_gates = (ui_acceptance or {}).get("gates") or {}

        alias = {
            "real_ashare_smoke": "approved_proxy_real_ashare_smoke",
            "twenty_session_shadow_replay": "twenty_session_shadow_replay",
            "security_authority": "security_authority",
            "recoverability_reproducibility": "recoverability_reproducibility",
            "offline_end_to_end": "offline_end_to_end",
        }
        for old_gate, current_gate in alias.items():
            previous = _gate_status(final_gates.get(old_gate))
            current = current_gates.get(current_gate, UNAVAILABLE)
            if previous and previous != current:
                conflicts.append(
                    AcceptanceConflict(
                        conflict_id=f"final_acceptance.{old_gate}",
                        artifact=self.FINAL_ACCEPTANCE_PATH,
                        gate=current_gate,
                        stale=current == IMPLEMENTED_AND_TESTED,
                        previous_status=previous,
                        current_status=current,
                        reason=(
                            "Older final acceptance was produced before C8 "
                            "approved-proxy/replay/security evidence."
                        ),
                    )
                )

        for old_gate, current_gate in alias.items():
            previous = _gate_status(ui_gates.get(old_gate))
            current = current_gates.get(current_gate, UNAVAILABLE)
            if previous and previous != current:
                conflicts.append(
                    AcceptanceConflict(
                        conflict_id=f"ui_acceptance.{old_gate}",
                        artifact=self.UI_ACCEPTANCE_PATH,
                        gate=current_gate,
                        stale=current == IMPLEMENTED_AND_TESTED,
                        previous_status=previous,
                        current_status=current,
                        reason=(
                            "UI/operator acceptance blocker is stale for the "
                            "standalone VeriQuant-only C8 scope but remains "
                            "visible until final acceptance is rebased."
                        ),
                    )
                )
        return conflicts

    def _validate_recorded_hashes(
        self,
        state: dict[str, Any] | None,
        actual_hashes: dict[str, str],
        blockers: list[dict[str, str]],
    ) -> None:
        if state is None:
            return
        recorded = state.get("artifact_hashes") or {}
        for rel_path in self.CRITICAL_HASH_PATHS:
            expected = _normalize_hash(recorded.get(rel_path))
            actual = actual_hashes.get(rel_path)
            if not actual:
                blockers.append(
                    _blocker(
                        AcceptanceReconcileFailureCode.MISSING_ARTIFACT,
                        rel_path,
                        "Critical artifact is missing.",
                    )
                )
                continue
            if not expected:
                blockers.append(
                    _blocker(
                        AcceptanceReconcileFailureCode.MISSING_ARTIFACT_HASH,
                        rel_path,
                        "Critical artifact hash is not recorded in state.json.",
                    )
                )
                continue
            if expected != actual:
                blockers.append(
                    _blocker(
                        AcceptanceReconcileFailureCode.HASH_MISMATCH,
                        rel_path,
                        f"state.json records {expected}, but the file hashes to {actual}.",
                    )
                )

    def _load_json(
        self,
        rel_path: str,
        blockers: list[dict[str, str]],
        *,
        required: bool,
    ) -> dict[str, Any] | None:
        path = self.repo_root / rel_path
        if not path.exists():
            if required:
                blockers.append(
                    _blocker(
                        AcceptanceReconcileFailureCode.MISSING_ARTIFACT,
                        rel_path,
                        "Required JSON artifact does not exist.",
                    )
                )
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            blockers.append(
                _blocker(
                    AcceptanceReconcileFailureCode.INVALID_JSON,
                    rel_path,
                    f"Invalid JSON: {exc.msg}.",
                )
            )
            return None
        if not isinstance(data, dict):
            blockers.append(
                _blocker(
                    AcceptanceReconcileFailureCode.INVALID_JSON,
                    rel_path,
                    "JSON artifact must be an object.",
                )
            )
            return None
        return data

    def _load_text(
        self,
        rel_path: str,
        blockers: list[dict[str, str]],
        *,
        required: bool,
    ) -> str | None:
        path = self.repo_root / rel_path
        if not path.exists():
            if required:
                blockers.append(
                    _blocker(
                        AcceptanceReconcileFailureCode.MISSING_ARTIFACT,
                        rel_path,
                        "Required text artifact does not exist.",
                    )
                )
            return None
        return path.read_text(encoding="utf-8")

    def _file_hash(self, rel_path: str) -> str | None:
        path = self.repo_root / rel_path
        if not path.exists() or not path.is_file():
            return None
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _parallel_result_files(self) -> Iterable[Path]:
        parallel_root = self.repo_root / ".autobuild/veriquant/parallel"
        if not parallel_root.exists():
            return ()
        return tuple(sorted(parallel_root.glob("*/result.json")))

    def _parallel_ready_packages(self) -> Iterable[str]:
        ready: list[str] = []
        for path in self._parallel_result_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if data.get("status") == "READY_FOR_INTEGRATION":
                ready.append(path.parent.name)
        return tuple(sorted(ready))

    def _has_forbidden_official_source(self, value: Any) -> bool:
        for key, item in _walk_key_values(value):
            lower_key = key.lower()
            if any(token in lower_key for token in ("source", "vendor", "provider")):
                if isinstance(item, str) and item == OFFICIAL_TUSHARE:
                    return True
        return False


def render_markdown(verdict: AcceptanceReconcileVerdict) -> str:
    data = verdict.to_dict()
    lines = [
        "# Acceptance Reconcile",
        "",
        f"- schema_version: `{data['schema_version']}`",
        f"- verdict: `{data['verdict']}`",
        f"- report_hash: `{data['report_hash']}`",
        "",
        "## Current V1 gate read model",
        "",
        "| Gate | Status |",
        "|---|---|",
    ]
    for gate, status in sorted(verdict.gates.items()):
        lines.append(f"| `{gate}` | `{status}` |")
    lines.extend(
        [
            "",
            "## Evidence snapshot",
            "",
            f"- final_acceptance_status: `{verdict.snapshot.final_acceptance_status}`",
            f"- ui_acceptance_status: `{verdict.snapshot.ui_acceptance_status}`",
            f"- c8_status: `{verdict.snapshot.c8_status}`",
            f"- c8_data_source_class: `{verdict.snapshot.c8_data_source_class}`",
            f"- c8_source_class: `{verdict.snapshot.c8_source_class}`",
            f"- c8_official_tushare_source: `{verdict.snapshot.c8_official_tushare_source}`",
            f"- c8_snapshot_hash: `{verdict.snapshot.c8_snapshot_hash}`",
            f"- c8_calendar_hash: `{verdict.snapshot.c8_calendar_hash}`",
            f"- c8_universe_hash: `{verdict.snapshot.c8_universe_hash}`",
            f"- c8_shadow_session_count: `{verdict.snapshot.c8_shadow_session_count}`",
            f"- c8_daily_vintage_count: `{verdict.snapshot.c8_daily_vintage_count}`",
            "",
            "## Conflicts",
            "",
        ]
    )
    if verdict.conflicts:
        lines.extend(["| Conflict | Artifact | Gate | Stale | Previous | Current |", "|---|---|---|---:|---|---|"])
        for conflict in verdict.conflicts:
            lines.append(
                "| `{}` | `{}` | `{}` | `{}` | `{}` | `{}` |".format(
                    conflict.conflict_id,
                    conflict.artifact,
                    conflict.gate,
                    str(conflict.stale).lower(),
                    conflict.previous_status,
                    conflict.current_status,
                )
            )
    else:
        lines.append("No conflicts detected.")
    lines.extend(["", "## Blockers", ""])
    if verdict.blockers:
        lines.extend(["| Code | Artifact | Message |", "|---|---|---|"])
        for blocker in verdict.blockers:
            lines.append(
                "| `{}` | `{}` | {} |".format(
                    blocker.get("code", ""),
                    blocker.get("artifact", ""),
                    blocker.get("message", ""),
                )
            )
    else:
        lines.append("No fail-closed blockers.")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "This artifact is read-only. It can recommend `NEEDS_FINAL_ACCEPTANCE_REBASE` "
                "or `READY_FOR_FINAL_ACCEPTANCE`, but it never marks global V1 as PASSED and "
                "does not edit `.autobuild/veriquant/state.json`."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _blocker(
    code: AcceptanceReconcileFailureCode, artifact: str, message: str
) -> dict[str, str]:
    return {"code": code.value, "artifact": artifact, "message": message}


def _status(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    for key in ("status", "overall_status", "verdict"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _gate_status(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        status = value.get("status")
        return status if isinstance(status, str) else None
    return None


def _state_has_successful_test(state: dict[str, Any] | None, needle: str) -> bool:
    if not state:
        return False
    tests = state.get("tests")
    if isinstance(tests, dict):
        items = tests.items()
    elif isinstance(tests, list):
        items = enumerate(tests)
    else:
        items = ()
    for key, value in items:
        text = json.dumps({str(key): value}, ensure_ascii=False, sort_keys=True).lower()
        if needle.lower() in text and (
            "passed" in text or IMPLEMENTED_AND_TESTED.lower() in text
        ):
            return True
    return False


def _walk_key_values(value: Any, parent_key: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            key_str = str(key)
            yield key_str, item
            yield from _walk_key_values(item, key_str)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_key_values(item, parent_key)


def _normalize_hash(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.lower()
    return normalized if normalized.startswith("sha256:") else "sha256:" + normalized


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return value


def _without_keys(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_keys(item, keys)
            for key, item in value.items()
            if key not in keys
        }
    if isinstance(value, list):
        return [_without_keys(item, keys) for item in value]
    if isinstance(value, tuple):
        return tuple(_without_keys(item, keys) for item in value)
    return value
