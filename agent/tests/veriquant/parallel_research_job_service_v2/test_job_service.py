from __future__ import annotations

import json

import pytest

from veriquant.contracts import ContractValidationError, FailureCode
from veriquant.data import canonical_content_hash
from veriquant.research_job_service_v2 import (
    ConfirmationAuthority,
    FakeBoundedStageProvider,
    JobState,
    ProviderMode,
    ResearchJobDraft,
    ResearchJobService,
    ResearchJobStage,
    ResearchReportReference,
)


def h(value: str) -> str:
    return canonical_content_hash({"value": value})


def stage(stage_id: str, branch: str, ordinal: int, *dependencies: str) -> ResearchJobStage:
    return ResearchJobStage(
        stage_id=stage_id,
        branch_id=branch,
        ordinal=ordinal,
        input_hash=h(f"input.{stage_id}"),
        config_hash=h("config.v1"),
        code_hash=h("code.v1"),
        policy_hash=h("policy.v1"),
        depends_on_stage_ids=tuple(dependencies),
    )


def draft(run_id: str = "job-one", frozen: str = "frozen.v1") -> ResearchJobDraft:
    return ResearchJobDraft(
        run_id=run_id,
        question="研究最近五十日动量并给出可追溯结论",
        campaign_draft_hash=h("campaign.draft"),
        frozen_input_hash=h(frozen),
        stages=(
            stage("planning", "control", 0),
            stage("group_a", "branch_a", 10, "planning"),
            stage("group_b", "branch_b", 11, "planning"),
            stage("report", "control", 20, "group_a", "group_b"),
        ),
    )


def service(tmp_path, *, primary=None, fallback=None):
    return ResearchJobService(
        root=tmp_path,
        primary_provider=primary,
        deterministic_fallback_provider=fallback or FakeBoundedStageProvider(),
    )


def confirm(job: ResearchJobService, run_id: str = "job-one"):
    return job.confirm(run_id, campaign_hash=h("campaign.confirmed"), authority=ConfirmationAuthority.USER)


def test_unconfirmed_draft_cannot_start(tmp_path):
    job = service(tmp_path)
    status = job.create_draft(draft())
    assert status.state is JobState.DRAFT
    with pytest.raises(ContractValidationError) as exc:
        job.start("job-one")
    assert exc.value.code is FailureCode.INVALID_STATE_TRANSITION


def test_only_explicit_human_authority_can_confirm(tmp_path):
    job = service(tmp_path)
    job.create_draft(draft())
    with pytest.raises(ContractValidationError) as exc:
        job.confirm("job-one", campaign_hash=h("campaign.confirmed"), authority="LLM")  # type: ignore[arg-type]
    assert exc.value.code is FailureCode.UNAUTHORIZED_CAPABILITY


def test_confirm_start_and_attach_report_reference(tmp_path):
    fallback = FakeBoundedStageProvider()
    job = service(tmp_path, fallback=fallback)
    job.create_draft(draft())
    assert confirm(job).state is JobState.CONFIRMED
    finished = job.start("job-one")
    assert finished.state is JobState.COMPLETED
    assert fallback.calls == ["planning", "group_a", "group_b", "report"]
    reference = ResearchReportReference(
        run_id="job-one",
        report_hash=h("report"),
        manifest_hash=h("publication.manifest"),
        relative_root="job-one",
    )
    attached = job.attach_report(reference)
    assert attached.report_reference == reference


def test_interruption_resume_after_process_rebuild_skips_committed_provider_stage(tmp_path):
    first_provider = FakeBoundedStageProvider(interrupt_stage_ids=frozenset({"group_a"}))
    first = service(tmp_path, fallback=first_provider)
    first.create_draft(draft())
    confirm(first)
    interrupted = first.start("job-one")
    assert interrupted.state is JobState.INTERRUPTED
    assert first_provider.calls == ["planning", "group_a"]

    # Simulate a new worker process. The rebuilt service reads persisted
    # checkpoints, so the already committed planning provider stage is not
    # invoked a second time.
    second_provider = FakeBoundedStageProvider()
    rebuilt = service(tmp_path, fallback=second_provider)
    completed = rebuilt.resume("job-one")
    assert completed.state is JobState.COMPLETED
    assert "planning" not in second_provider.calls
    assert second_provider.calls == ["group_a", "group_b", "report"]

    calls_before_replay = list(second_provider.calls)
    replay = rebuilt.resume("job-one")
    assert replay.state is JobState.COMPLETED
    assert second_provider.calls == calls_before_replay


def test_sibling_branch_continues_when_independent_branch_fails(tmp_path):
    fallback = FakeBoundedStageProvider(fail_stage_ids=frozenset({"group_a"}))
    job = service(tmp_path, fallback=fallback)
    job.create_draft(draft())
    confirm(job)
    result = job.start("job-one")
    assert result.state is JobState.PARTIAL
    assert fallback.calls == ["planning", "group_a", "group_b"]
    tasks = job.store.load_manifest("job-one").to_tasks()
    by_name = {task.identity.stage_id: task.identity.artifact_hash for task in tasks}
    assert by_name["group_b"] in result.committed_stage_hashes
    assert by_name["group_a"] in result.failed_stage_hashes
    assert by_name["report"] in result.blocked_stage_hashes


def test_provider_unavailable_uses_explicit_deterministic_fallback(tmp_path):
    primary = FakeBoundedStageProvider(unavailable_stage_ids=frozenset({"planning", "group_a", "group_b", "report"}))
    fallback = FakeBoundedStageProvider()
    job = service(tmp_path, primary=primary, fallback=fallback)
    job.create_draft(draft())
    confirm(job)
    result = job.start("job-one")
    assert result.state is JobState.COMPLETED
    assert ProviderMode.DETERMINISTIC_FALLBACK in result.provider_modes
    assert set(primary.calls) == {"planning", "group_a", "group_b", "report"}
    assert fallback.calls == ["planning", "group_a", "group_b", "report"]


def test_cancel_is_durable_and_cannot_resume(tmp_path):
    job = service(tmp_path)
    job.create_draft(draft())
    confirm(job)
    assert job.cancel("job-one").state is JobState.CANCELLED
    rebuilt = service(tmp_path)
    assert rebuilt.status("job-one").state is JobState.CANCELLED
    with pytest.raises(ContractValidationError) as exc:
        rebuilt.resume("job-one")
    assert exc.value.code is FailureCode.INVALID_STATE_TRANSITION


def test_same_run_id_with_changed_input_fails_closed(tmp_path):
    job = service(tmp_path)
    job.create_draft(draft())
    with pytest.raises(ContractValidationError) as exc:
        job.create_draft(draft(frozen="frozen.changed"))
    assert exc.value.code is FailureCode.ARTIFACT_COLLISION


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "a\\b", "C:escape"])
def test_path_traversal_run_ids_are_rejected(tmp_path, run_id):
    with pytest.raises(ContractValidationError) as exc:
        service(tmp_path).create_draft(draft(run_id=run_id))
    assert exc.value.code in {FailureCode.UNAUTHORIZED_CAPABILITY, FailureCode.INVALID_IDENTIFIER}


def test_append_only_event_corruption_fails_closed(tmp_path):
    job = service(tmp_path)
    job.create_draft(draft())
    event_path = job.store.run_dir("job-one") / "events.jsonl"
    lines = event_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(lines[0])
    payload["payload"]["draft_hash"] = h("tampered")
    event_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with pytest.raises(ContractValidationError) as exc:
        job.status("job-one")
    assert exc.value.code is FailureCode.LEDGER_CORRUPTION


def test_report_path_cannot_escape_run_root(tmp_path):
    with pytest.raises(ContractValidationError):
        ResearchReportReference(
            run_id="job-one",
            report_hash=h("report"),
            manifest_hash=h("manifest"),
            relative_root="../outside",
        )


def test_package_contains_no_public_route_or_secret_behavior():
    import pathlib
    import veriquant.research_job_service_v2 as package

    root = pathlib.Path(package.__file__).parent
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "FastAPI(" not in source
    assert "include_router" not in source
    assert "os.environ" not in source
    assert "api_server" not in source
    assert "broker" not in source.lower()
    assert "live route" not in source.lower()
    assert "eval(" not in source
    assert "exec(" not in source
