from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from src.veriquant.operator_api.api import (
    InMemoryOperatorJobStore,
    OperatorApiError,
    OperatorApiRoutes,
    OperatorBudget,
    OperatorJobService,
    OperatorJobStatus,
    OperatorRegistry,
    RegisteredProfile,
    RegisteredSnapshot,
    build_operator_route_manifest,
)


def registry() -> OperatorRegistry:
    return OperatorRegistry(
        snapshots={
            "snap-a": RegisteredSnapshot(snapshot_id="snap-a", content_hash="sha256:" + ("a" * 64)),
        },
        profiles={
            "profile-a": RegisteredProfile(
                profile_id="profile-a",
                max_budget=OperatorBudget(max_steps=5, max_events=20, max_wall_clock_seconds=30),
            )
        },
    )


def routes() -> OperatorApiRoutes:
    return OperatorApiRoutes(OperatorJobService(registry=registry(), store=InMemoryOperatorJobStore()))


def valid_payload():
    return {
        "snapshot_id": "snap-a",
        "profile_id": "profile-a",
        "seed": 7,
        "budget": {"max_steps": 3, "max_events": 10, "max_wall_clock_seconds": 15},
    }


def test_create_job_accepts_only_registered_snapshot_profile_seed_and_budget() -> None:
    api = routes()
    created = api.post_jobs(valid_payload(), idempotency_key="idem-1")

    assert created["status"] == OperatorJobStatus.QUEUED.value
    assert created["snapshot_id"] == "snap-a"
    assert created["profile_id"] == "profile-a"
    assert created["seed"] == 7

    with pytest.raises(OperatorApiError):
        api.post_jobs({**valid_payload(), "snapshot_id": "missing"}, idempotency_key="idem-2")
    with pytest.raises(OperatorApiError):
        api.post_jobs({**valid_payload(), "profile_id": "missing"}, idempotency_key="idem-3")
    with pytest.raises(OperatorApiError):
        api.post_jobs({**valid_payload(), "budget": {"max_steps": 6}}, idempotency_key="idem-4")


@pytest.mark.parametrize(
    "extra",
    [
        {"url": "https://example.invalid/data.json"},
        {"path": "../sealed/artifact.json"},
        {"tushare_token": "Bearer abcdefghi123456789"},
        {"python": "print('run arbitrary code')"},
        {"shell": "powershell Get-ChildItem Env:"},
        {"provider": "arbitrary-provider"},
        {"promotion_verdict": "PROMOTE_CHALLENGER"},
        {"budget": {"max_steps": 1, "cmd": "bash run.sh"}},
    ],
)
def test_forbidden_url_path_token_python_shell_provider_and_promotion_fields_are_rejected(extra) -> None:
    with pytest.raises(OperatorApiError):
        routes().post_jobs({**valid_payload(), **extra}, idempotency_key="blocked-1")


def test_ui_schema_does_not_read_or_submit_tushare_tokens() -> None:
    schema = build_operator_route_manifest().routes[0].request_schema
    encoded = json.dumps(schema, sort_keys=True).lower()

    assert "tushare_token" not in encoded
    assert "api_key" not in encoded
    assert schema["additionalProperties"] is False
    assert "must not read" in schema["x-veriquant-ui-policy"]["tokens"].lower()


def test_idempotency_key_repeat_does_not_create_duplicate_job_or_events() -> None:
    api = routes()
    first = api.post_jobs(valid_payload(), idempotency_key="stable-key")
    second = api.post_jobs(valid_payload(), idempotency_key="stable-key")

    assert second["job_id"] == first["job_id"]
    assert api.service.store.job_count() == 1
    assert len(api.get_job_events(first["job_id"])["events"]) == 1

    with pytest.raises(OperatorApiError):
        api.post_jobs({**valid_payload(), "seed": 8}, idempotency_key="stable-key")


def test_events_are_append_only_and_cursor_queries_are_stable() -> None:
    api = routes()
    job = api.post_jobs(valid_payload(), idempotency_key="event-key")
    job_id = job["job_id"]

    first_page = api.get_job_events(job_id)
    assert [event["sequence"] for event in first_page["events"]] == [1]
    assert first_page["next_cursor"] == "1"

    api.post_resume(job_id)
    second_page_a = api.get_job_events(job_id, after="1")
    second_page_b = api.get_job_events(job_id, after="1")
    assert [event["sequence"] for event in second_page_a["events"]] == [2]
    assert second_page_a == second_page_b


def test_cancel_and_resume_legal_transitions_and_illegal_transitions_fail_closed() -> None:
    api = routes()
    job_id = api.post_jobs(valid_payload(), idempotency_key="fsm-key")["job_id"]

    cancelled = api.post_cancel(job_id)
    assert cancelled["status"] == OperatorJobStatus.CANCELLED.value

    with pytest.raises(OperatorApiError):
        api.post_cancel(job_id)

    resumed = api.post_resume(job_id)
    assert resumed["status"] == OperatorJobStatus.RUNNING.value

    with pytest.raises(OperatorApiError):
        api.post_resume(job_id)


def test_route_manifest_excludes_live_privileged_routes_and_requires_loopback() -> None:
    manifest = build_operator_route_manifest(bind_host="127.0.0.1")
    route_blob = " ".join(f"{route.method} {route.path} {route.authority} {route.handler}" for route in manifest.routes).lower()

    assert "/live" not in route_blob
    assert "broker" not in route_blob
    assert "order" not in route_blob
    assert "oauth" not in route_blob
    assert manifest.config.bind_host == "127.0.0.1"

    build_operator_route_manifest(bind_host="localhost")
    with pytest.raises(Exception):
        build_operator_route_manifest(bind_host="0.0.0.0")


def test_route_manifest_uses_polling_not_sse() -> None:
    manifest = build_operator_route_manifest()

    assert manifest.config.sse_enabled is False
    assert any(route.path == "/veriquant/jobs/{id}/events" and route.method == "GET" for route in manifest.routes)


def test_error_payloads_do_not_output_environment_or_secret_like_values() -> None:
    api = routes()
    with pytest.raises(OperatorApiError) as exc:
        api.post_jobs({**valid_payload(), "token": "Bearer very-secret-value-123456789"}, idempotency_key="secret-key")

    encoded = json.dumps(exc.value.to_dict(), sort_keys=True)
    assert "very-secret" not in encoded
    assert "PATH=" not in encoded
    assert "USERNAME=" not in encoded
    assert "ALLUSERSPROFILE" not in encoded
