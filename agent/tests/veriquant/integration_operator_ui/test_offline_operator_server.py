from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import veriquant_offline_operator_server


ROOT = Path(__file__).resolve().parents[4]
def _module():
    return veriquant_offline_operator_server


def _client(tmp_path: Path):
    module = _module()
    app = module.create_app(
        artifact_root=ROOT / ".autobuild/veriquant/integration/workbench_artifacts",
        job_root=tmp_path / "jobs",
        real_run_root=tmp_path / "real-runs",
        allow_test_host=True,
    )
    return module, TestClient(app)


def test_route_manifest_is_closed_and_has_no_live_surface(tmp_path: Path) -> None:
    module, client = _client(tmp_path)
    manifest = module.build_route_manifest(client.app)
    actual = {(route.method, route.path) for route in manifest.routes}
    assert actual == set(module.EXPECTED_ROUTES)
    assert all(not route.financial_authority for route in manifest.routes)
    assert not any(token in route.path.lower() for route in manifest.routes for token in ("/live", "broker", "order", "oauth", "mandate"))


def test_loopback_origin_and_operator_intent_fail_closed(tmp_path: Path) -> None:
    _, client = _client(tmp_path)
    payload = {"profile_id": "control-smoke-v1", "seed": 7, "budget": 3, "idempotency_key": "intent-test-key"}
    assert client.post("/veriquant/jobs", json=payload).status_code == 403
    assert client.post(
        "/veriquant/jobs",
        json=payload,
        headers={"X-VeriQuant-Operator": "offline-ui-v1", "Origin": "https://evil.example"},
    ).status_code == 403
    outside = TestClient(client.app, base_url="http://external.example")
    assert outside.get("/healthz").status_code == 403


def test_control_job_create_status_events_and_idempotency(tmp_path: Path) -> None:
    _, client = _client(tmp_path)
    headers = {"X-VeriQuant-Operator": "offline-ui-v1"}
    profiles = client.get("/veriquant/operator/profiles").json()["profiles"]
    assert {item["profile_id"] for item in profiles} == {
        "control-smoke-v1",
        "approved-proxy-visible-evidence-v1",
    }
    payload = {"profile_id": "control-smoke-v1", "seed": 7, "budget": 3, "idempotency_key": "control-idempotency"}
    first = client.post("/veriquant/jobs", json=payload, headers=headers)
    assert first.status_code == 200
    job = first.json()["job"]
    assert job["status"] in {"PENDING", "COMPLETED"}
    status = client.get(f"/veriquant/jobs/{job['job_id']}").json()["job"]
    assert status["status"] == "COMPLETED"
    duplicate = client.post("/veriquant/jobs", json=payload, headers=headers).json()["job"]
    assert duplicate["job_id"] == job["job_id"]
    events = client.get(f"/veriquant/jobs/{job['job_id']}/events?after=0&limit=200").json()
    assert events["next_cursor"] == len(events["events"])
    assert events["events"][-1]["event_type"] == "RUN_COMPLETED"


def test_real_visible_evidence_profile_runs_pinned_offline_chain(tmp_path: Path) -> None:
    _, client = _client(tmp_path)
    payload = {
        "profile_id": "approved-proxy-visible-evidence-v1",
        "seed": 7,
        "budget": 2,
        "idempotency_key": "real-visible-evidence",
    }
    response = client.post("/veriquant/jobs", json=payload, headers={"X-VeriQuant-Operator": "offline-ui-v1"})
    assert response.status_code == 200
    job_id = response.json()["job"]["job_id"]
    status = client.get(f"/veriquant/jobs/{job_id}").json()["job"]
    assert status["status"] == "COMPLETED"
    assert set(status["artifact_hashes"]) == {"factor_dag_pit_official_evidence"}
    assert (tmp_path / "real-runs" / job_id / "official_evidence_run.json").is_file()


def test_workbench_direct_call_query_default_is_compatible(tmp_path: Path) -> None:
    _, client = _client(tmp_path)
    response = client.get("/veriquant/workbench/runs")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
