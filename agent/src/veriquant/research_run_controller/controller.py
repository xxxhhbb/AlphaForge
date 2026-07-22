"""Research OS run controller built on the generic recovery store."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.contracts.base import canonicalize, fail, require_hash, require_identifier
from veriquant.data import canonical_content_hash
from veriquant.research_os import CandidateValidationRequest, ResearchGroupSubmission, ResearchRoutePlan
from veriquant.run_recovery.api import (
    ProviderUnavailable,
    RecoveryError,
    RecoveryInterrupted,
    RecoveryStatus,
    RunCheckpointStore,
    RunRecoveryController,
    StageExecutionResult,
    StageTask,
)

from .models import (
    ResearchRunExecutionSummary,
    ResearchRunLifecycle,
    ResearchRunManifest,
    ResearchRunPlan,
    ResearchRunStage,
    ResearchRunStageKind,
)


DEFAULT_CLOCK = datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc)
ZERO_HASH = "sha256:" + "0" * 64


_MANIFEST_LOCKS_GUARD = threading.Lock()
_MANIFEST_LOCKS: dict[str, threading.RLock] = {}


def _manifest_lock(path: Path) -> threading.RLock:
    """Return one process-wide lock for a canonical manifest path.

    Control, multi-specialist, reporting, and product services construct their
    own identity-store instances.  An instance-local lock therefore cannot
    fence a stale finalizer from a concurrent cancellation.
    """

    key = os.path.normcase(str(path.resolve()))
    with _MANIFEST_LOCKS_GUARD:
        return _MANIFEST_LOCKS.setdefault(key, threading.RLock())


def _stable_hash(value: Any) -> str:
    rendered = json.dumps(canonicalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _safe_segment(value: str, field_name: str) -> None:
    require_identifier(value, field_name)
    if any(token in value for token in ("..", "/", "\\", ":")):
        fail(FailureCode.UNAUTHORIZED_CAPABILITY, f"{field_name} must be a safe segment")


def _short(hash_value: str) -> str:
    require_hash(hash_value, "hash_value")
    return hash_value.removeprefix("sha256:")[:12]


def _stage(
    *,
    stage_key: str,
    stage_kind: ResearchRunStageKind,
    branch_id: str,
    ordinal: int,
    input_hash: str,
    config_hash: str,
    code_hash: str,
    policy_hash: str,
    depends_on_stage_keys: tuple[str, ...] = (),
    provider_id: str = "research_stage_provider",
) -> ResearchRunStage:
    expected_output_hash = _stable_hash(
        {
            "stage_key": stage_key,
            "stage_kind": stage_kind.value,
            "branch_id": branch_id,
            "ordinal": ordinal,
            "input_hash": input_hash,
            "config_hash": config_hash,
            "code_hash": code_hash,
            "policy_hash": policy_hash,
            "depends_on_stage_keys": depends_on_stage_keys,
        }
    )
    return ResearchRunStage(
        stage_key=stage_key,
        stage_kind=stage_kind,
        branch_id=branch_id,
        ordinal=ordinal,
        input_hash=input_hash,
        config_hash=config_hash,
        code_hash=code_hash,
        policy_hash=policy_hash,
        expected_output_hash=expected_output_hash,
        provider_id=provider_id,
        depends_on_stage_keys=depends_on_stage_keys,
    )


def build_research_run_plan(
    *,
    run_id: str,
    campaign_hash: str,
    intent_hash: str,
    data_plan_hash: str,
    route_plan: ResearchRoutePlan,
    group_submissions: Sequence[ResearchGroupSubmission],
    validation_requests: Sequence[CandidateValidationRequest],
    policy_hash: str,
    code_hash: str,
    created_at: datetime = DEFAULT_CLOCK,
) -> ResearchRunPlan:
    """Create stable stage identities for one Research OS run."""

    _safe_segment(run_id, "run_id")
    if not isinstance(route_plan, ResearchRoutePlan):
        fail(FailureCode.INVALID_VALUE, "route_plan must be ResearchRoutePlan")
    submissions = tuple(group_submissions)
    requests = tuple(validation_requests)
    if any(not isinstance(item, ResearchGroupSubmission) for item in submissions):
        fail(FailureCode.INVALID_VALUE, "group_submissions must contain ResearchGroupSubmission")
    if any(not isinstance(item, CandidateValidationRequest) for item in requests):
        fail(FailureCode.INVALID_VALUE, "validation_requests must contain CandidateValidationRequest")
    selected_groups = tuple(assignment.group_id for assignment in route_plan.assignments)
    selected_set = set(selected_groups)
    submission_by_group = {item.group_id: item for item in submissions}
    if len(submission_by_group) != len(submissions):
        fail(FailureCode.INVALID_VALUE, "duplicate research group submission")
    for group_id in submission_by_group:
        if group_id not in selected_set:
            fail(FailureCode.INVALID_STATE, "submission group was not selected")
    for request in requests:
        if request.group_id not in submission_by_group:
            fail(FailureCode.INVALID_STATE, "candidate validation requires a group submission first")
    manifest = ResearchRunManifest(
        run_id=run_id,
        campaign_hash=campaign_hash,
        intent_hash=intent_hash,
        data_plan_hash=data_plan_hash,
        route_plan_hash=route_plan.artifact_hash,
        policy_hash=policy_hash,
        code_hash=code_hash,
        selected_group_ids=selected_groups,
        candidate_request_hashes=tuple(sorted(request.artifact_hash for request in requests)),
        created_at=created_at,
    )
    stages: list[ResearchRunStage] = []
    planning = _stage(
        stage_key="planning",
        stage_kind=ResearchRunStageKind.PLANNING,
        branch_id="control",
        ordinal=0,
        input_hash=_stable_hash({"manifest": manifest.artifact_hash, "route": route_plan.artifact_hash}),
        config_hash=route_plan.artifact_hash,
        code_hash=code_hash,
        policy_hash=policy_hash,
    )
    stages.append(planning)
    validation_stage_keys: list[str] = []
    for index, group_id in enumerate(selected_groups, start=1):
        group_branch = f"group_{group_id}"
        group_stage = _stage(
            stage_key=f"{group_branch}_selected",
            stage_kind=ResearchRunStageKind.SELECTED_GROUP,
            branch_id=group_branch,
            ordinal=10 + index,
            input_hash=route_plan.artifact_hash,
            config_hash=manifest.artifact_hash,
            code_hash=code_hash,
            policy_hash=policy_hash,
            depends_on_stage_keys=(planning.stage_key,),
        )
        stages.append(group_stage)
        submission = submission_by_group.get(group_id)
        if submission is None:
            continue
        translation_stage = _stage(
            stage_key=f"{group_branch}_translation",
            stage_kind=ResearchRunStageKind.TRANSLATION,
            branch_id=group_branch,
            ordinal=30 + index,
            input_hash=submission.artifact_hash,
            config_hash=manifest.artifact_hash,
            code_hash=code_hash,
            policy_hash=policy_hash,
            depends_on_stage_keys=(group_stage.stage_key,),
        )
        stages.append(translation_stage)
        group_requests = tuple(sorted((item for item in requests if item.group_id == group_id), key=lambda item: item.artifact_hash))
        for offset, validation in enumerate(group_requests, start=1):
            candidate_key = f"candidate_{_short(validation.candidate_draft_hash)}_validation"
            validation_stage = _stage(
                stage_key=candidate_key,
                stage_kind=ResearchRunStageKind.CANDIDATE_VALIDATION,
                branch_id=group_branch,
                ordinal=50 + index * 10 + offset,
                input_hash=validation.artifact_hash,
                config_hash=validation.protocol_hash,
                code_hash=code_hash,
                policy_hash=policy_hash,
                depends_on_stage_keys=(translation_stage.stage_key,),
            )
            stages.append(validation_stage)
            validation_stage_keys.append(validation_stage.stage_key)
    tournament_deps = tuple(validation_stage_keys)
    tournament = _stage(
        stage_key="candidate_tournament",
        stage_kind=ResearchRunStageKind.TOURNAMENT,
        branch_id="control",
        ordinal=900,
        input_hash=_stable_hash({"candidate_request_hashes": manifest.candidate_request_hashes}),
        config_hash=route_plan.artifact_hash,
        code_hash=code_hash,
        policy_hash=policy_hash,
        depends_on_stage_keys=tournament_deps,
    )
    report = _stage(
        stage_key="formal_report",
        stage_kind=ResearchRunStageKind.REPORT,
        branch_id="control",
        ordinal=1000,
        input_hash=tournament.expected_output_hash,
        config_hash=manifest.artifact_hash,
        code_hash=code_hash,
        policy_hash=policy_hash,
        depends_on_stage_keys=(tournament.stage_key,),
    )
    stages.extend((tournament, report))
    return ResearchRunPlan(manifest=manifest, stages=tuple(stages))


class ResearchRunIdentityStore:
    """Atomic manifest guard for run_id/input idempotency."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def run_dir(self, run_id: str) -> Path:
        _safe_segment(run_id, "run_id")
        target = (self.root / "research_runs" / run_id).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise RecoveryError(FailureCode.UNAUTHORIZED_CAPABILITY, "research run path escapes root") from exc
        return target

    def ensure_manifest(
        self,
        manifest: ResearchRunManifest,
        *,
        expected_artifact_hash: str | None = None,
        allow_cancelled_resume: bool = False,
    ) -> ResearchRunManifest:
        """Atomically create or advance the canonical manifest.

        ``expected_artifact_hash`` is a compare-and-swap fence.  Callers which
        derive a new manifest from a previously loaded value must pass that
        value's artifact hash.  Terminal states are monotonic; the sole escape
        is an explicit CANCELLED -> PLANNING resume.
        """

        path = self.run_dir(manifest.run_id) / "manifest.json"
        with _manifest_lock(path):
            existing = self._load_path(path)
            if existing is None:
                if expected_artifact_hash is not None:
                    raise RecoveryError(
                        FailureCode.ARTIFACT_COLLISION,
                        "canonical research manifest is missing for compare-and-swap",
                        details={"run_id": manifest.run_id},
                    )
                _atomic_json(path, manifest.to_dict())
                return manifest
            if existing.identity_hash != manifest.identity_hash:
                raise RecoveryError(
                    FailureCode.ARTIFACT_COLLISION,
                    "same run_id already exists with different research input",
                    details={"run_id": manifest.run_id},
                )
            if existing.artifact_hash == manifest.artifact_hash:
                return existing
            if expected_artifact_hash is not None and existing.artifact_hash != expected_artifact_hash:
                raise RecoveryError(
                    FailureCode.ARTIFACT_COLLISION,
                    "stale research manifest update was fenced",
                    details={"run_id": manifest.run_id},
                )
            self._validate_update(existing, manifest, allow_cancelled_resume=allow_cancelled_resume)
            _atomic_json(path, manifest.to_dict())
            return manifest

    def load_manifest(self, run_id: str) -> ResearchRunManifest | None:
        path = self.run_dir(run_id) / "manifest.json"
        with _manifest_lock(path):
            return self._load_path(path)

    @staticmethod
    def _load_path(path: Path) -> ResearchRunManifest | None:
        if not path.exists():
            return None
        try:
            return ResearchRunManifest.from_payload(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise RecoveryError(FailureCode.LEDGER_CORRUPTION, "research run manifest is corrupt") from exc

    @staticmethod
    def _validate_update(
        existing: ResearchRunManifest,
        proposed: ResearchRunManifest,
        *,
        allow_cancelled_resume: bool,
    ) -> None:
        terminal = {
            ResearchRunLifecycle.COMPLETED,
            ResearchRunLifecycle.PARTIAL,
            ResearchRunLifecycle.BLOCKED,
            ResearchRunLifecycle.FAILED,
            ResearchRunLifecycle.CANCELLED,
        }
        allowed = {
            ResearchRunLifecycle.PLANNING: {
                ResearchRunLifecycle.PLANNING,
                ResearchRunLifecycle.RESEARCHING,
                ResearchRunLifecycle.CANDIDATE_PREFLIGHT,
                ResearchRunLifecycle.AWAITING_EVIDENCE,
                *terminal,
            },
            ResearchRunLifecycle.RESEARCHING: {
                ResearchRunLifecycle.RESEARCHING,
                ResearchRunLifecycle.CANDIDATE_PREFLIGHT,
                ResearchRunLifecycle.AWAITING_EVIDENCE,
                *terminal,
            },
            ResearchRunLifecycle.CANDIDATE_PREFLIGHT: {
                ResearchRunLifecycle.CANDIDATE_PREFLIGHT,
                ResearchRunLifecycle.AWAITING_EVIDENCE,
                *terminal,
            },
            ResearchRunLifecycle.AWAITING_EVIDENCE: {
                ResearchRunLifecycle.AWAITING_EVIDENCE,
                *terminal,
            },
        }
        is_resume = (
            existing.current_stage is ResearchRunLifecycle.CANCELLED
            and proposed.current_stage is ResearchRunLifecycle.PLANNING
            and allow_cancelled_resume
        )
        if not is_resume:
            if existing.current_stage in terminal:
                valid_state = proposed.current_stage is existing.current_stage or (
                    existing.current_stage in {
                        ResearchRunLifecycle.COMPLETED,
                        ResearchRunLifecycle.PARTIAL,
                        ResearchRunLifecycle.BLOCKED,
                    }
                    and proposed.current_stage is ResearchRunLifecycle.FAILED
                    and existing.report_hash is None
                )
            else:
                valid_state = proposed.current_stage in allowed[existing.current_stage]
            if not valid_state:
                raise RecoveryError(
                    FailureCode.INVALID_STATE,
                    "research manifest lifecycle cannot regress or replace a terminal state",
                    details={
                        "current_stage": existing.current_stage.value,
                        "proposed_stage": proposed.current_stage.value,
                    },
                )

        for field_name in ("candidate_count", "trial_count"):
            if getattr(proposed, field_name) < getattr(existing, field_name):
                raise RecoveryError(FailureCode.INVALID_STATE, f"{field_name} cannot decrease")
        for field_name in (
            "remaining_iterations",
            "remaining_tool_calls",
            "remaining_candidate_slots",
        ):
            if getattr(proposed, field_name) > getattr(existing, field_name):
                raise RecoveryError(FailureCode.INVALID_STATE, f"{field_name} cannot increase")

        existing_failures = set(existing.typed_failures)
        proposed_failures = set(proposed.typed_failures)
        if is_resume:
            if existing_failures - {"CANCELLED"} - proposed_failures:
                raise RecoveryError(FailureCode.INVALID_STATE, "resume cannot discard typed failures")
        elif not existing_failures.issubset(proposed_failures):
            raise RecoveryError(FailureCode.INVALID_STATE, "typed failures are append-only")

        for field_name in ("official_evidence_hash", "report_hash"):
            old_value = getattr(existing, field_name)
            new_value = getattr(proposed, field_name)
            if (
                proposed.current_stage is ResearchRunLifecycle.CANCELLED
                and field_name == "official_evidence_hash"
                and new_value is not None
            ):
                raise RecoveryError(FailureCode.INVALID_STATE, "cancelled runs cannot acquire official evidence")
            if is_resume and field_name == "report_hash":
                # A cancellation report remains archived by the report backend;
                # the resumed execution needs a fresh canonical report slot.
                continue
            if old_value is not None and new_value != old_value and proposed.current_stage not in {
                ResearchRunLifecycle.CANCELLED,
                ResearchRunLifecycle.FAILED,
            }:
                raise RecoveryError(FailureCode.INVALID_STATE, f"{field_name} cannot be cleared or replaced")


class ResearchRunController:
    """Research OS orchestration wrapper over the generic recovery controller."""

    def __init__(
        self,
        *,
        root: str | Path,
        providers: Mapping[str, Any],
        lease_ttl_seconds: int = 60,
    ) -> None:
        self.root = Path(root).resolve()
        self.identity_store = ResearchRunIdentityStore(self.root)
        self.recovery_store = RunCheckpointStore(self.root / "recovery")
        self.recovery = RunRecoveryController(
            store=self.recovery_store,
            providers=providers,
            lease_ttl_seconds=lease_ttl_seconds,
        )

    def plan(self, plan: ResearchRunPlan) -> ResearchRunExecutionSummary:
        self.identity_store.ensure_manifest(plan.manifest)
        resume = self.recovery.plan(run_id=plan.manifest.run_id, tasks=plan.to_stage_tasks())
        return self._summary(plan, resume)

    def run(self, plan: ResearchRunPlan, *, holder_id: str = "research_controller") -> ResearchRunExecutionSummary:
        self.identity_store.ensure_manifest(plan.manifest)
        resume = self.recovery.run(run_id=plan.manifest.run_id, tasks=plan.to_stage_tasks(), holder_id=holder_id)
        return self._summary(plan, resume)

    def cancel(self, plan: ResearchRunPlan, *, holder_id: str = "operator") -> ResearchRunExecutionSummary:
        self.identity_store.ensure_manifest(plan.manifest)
        resume = self.recovery.cancel(run_id=plan.manifest.run_id, holder_id=holder_id)
        return self._summary(plan, resume)

    def _summary(self, plan: ResearchRunPlan, resume: Any) -> ResearchRunExecutionSummary:
        events = self.recovery_store.read_events(plan.manifest.run_id)
        event_hash = events[-1].artifact_hash if events else None
        return ResearchRunExecutionSummary(
            run_id=plan.manifest.run_id,
            status=resume.status,
            manifest_hash=plan.manifest.artifact_hash,
            resume_plan_hash=resume.artifact_hash,
            committed_stage_hashes=resume.committed_stage_hashes,
            skipped_stage_hashes=resume.skipped_stage_hashes,
            runnable_stage_hashes=resume.runnable_stage_hashes,
            failed_stage_hashes=resume.failed_stage_hashes,
            blocked_stage_hashes=resume.blocked_stage_hashes,
            final_artifact_hash=resume.final_artifact_hash,
            event_log_hash=event_hash,
            message=resume.message,
        )


@dataclass
class FakeResearchStageProvider:
    """Test-only deterministic provider for Research OS stage orchestration."""

    fail_stage_keys: frozenset[str] = frozenset()
    unavailable_stage_keys: frozenset[str] = frozenset()
    interrupt_stage_keys: frozenset[str] = frozenset()
    interrupt_once: bool = True
    calls: list[str] = field(default_factory=list)
    _interrupted: set[str] = field(default_factory=set)

    def execute(self, task: StageTask, *, committed_inputs: Mapping[str, str]) -> StageExecutionResult:
        stage_key = task.identity.stage_id
        self.calls.append(stage_key)
        if stage_key in self.unavailable_stage_keys:
            raise ProviderUnavailable(FailureCode.ADAPTER_UNAVAILABLE, f"provider unavailable for {stage_key}")
        if stage_key in self.fail_stage_keys:
            raise RecoveryError(FailureCode.ADAPTER_EXECUTION_FAILED, f"stage failed closed: {stage_key}")
        if stage_key in self.interrupt_stage_keys and (not self.interrupt_once or stage_key not in self._interrupted):
            self._interrupted.add(stage_key)
            raise RecoveryInterrupted(FailureCode.ADAPTER_EXECUTION_FAILED, f"interrupted before commit: {stage_key}")
        payload = {
            "stage_identity_hash": task.identity.artifact_hash,
            "committed_inputs": tuple(sorted(committed_inputs)),
            "stage_key": stage_key,
        }
        return StageExecutionResult(
            stage_identity_hash=task.identity.artifact_hash,
            output_artifact_hash=task.identity.expected_artifact_hash,
            payload_hash=canonical_content_hash(payload),
            metadata={"stage_key": stage_key},
        )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = json.dumps(canonicalize(payload), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", delete=False, dir=str(path.parent))
    tmp_name = handle.name
    try:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_name, path)
    finally:
        if not handle.closed:
            handle.close()
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
