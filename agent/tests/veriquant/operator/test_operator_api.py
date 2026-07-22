from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def test_standalone_app_route_authority_and_loopback(tmp_path) -> None:
    module = importlib.import_module("veriquant_operator_api")
    app = module.create_app(root=tmp_path)
    manifest = module.build_route_manifest(app)

    assert manifest.offline_only is True
    assert manifest.loopback_only is True
    assert manifest.provider_keys_required is False
    assert all(route.financial_authority is False for route in manifest.routes)
    assert not any(
        token in route.path.lower()
        for route in manifest.routes
        for token in ("/live", "broker", "order", "oauth", "mandate", "sealed")
    )
    assert module.validate_bind_host("127.0.0.1") == "127.0.0.1"
    try:
        module.validate_bind_host("0.0.0.0")
    except ValueError:
        pass
    else:
        raise AssertionError("non-loopback bind must fail closed")


def test_api_create_poll_cancel_resume_and_closed_schema(tmp_path) -> None:
    module = importlib.import_module("veriquant_operator_api")
    client = TestClient(module.create_app(root=tmp_path))
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["provider_keys_required"] is False

    profiles = client.get("/veriquant/operator/profiles").json()["profiles"]
    assert [item["profile_id"] for item in profiles] == ["control-smoke-v1"]
    body = {"profile_id": "control-smoke-v1", "seed": 7, "budget": 2, "idempotency_key": "api-operator-key"}
    created = client.post("/veriquant/jobs", json=body)
    assert created.status_code == 200
    job = created.json()["job"]
    assert job["status"] == "PENDING"
    completed = client.get(f"/veriquant/jobs/{job['job_id']}").json()["job"]
    assert completed["status"] == "COMPLETED"
    events = client.get(f"/veriquant/jobs/{job['job_id']}/events", params={"after": 0, "limit": 2})
    assert events.status_code == 200
    assert len(events.json()["events"]) == 2

    rejected = client.post("/veriquant/jobs", json={**body, "promotion": "APPROVE"})
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "INVALID_REQUEST"
    cancel = client.post(f"/veriquant/jobs/{job['job_id']}/cancel")
    assert cancel.status_code == 409


def test_module_imports_no_live_or_broker_runtime() -> None:
    module = importlib.import_module("veriquant_operator_api")
    imports = " ".join(module.route_authority_manifest.imported_modules).lower()
    assert "live_routes" not in imports
    assert "broker" not in imports
