from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import uvicorn

import alphaforge_api_server as server


def test_main_imports_the_verified_module_application(monkeypatch):
    captured = {}

    def fake_run(application, **kwargs):
        captured["application"] = application
        captured["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)

    assert server.main(["--host", "127.0.0.1", "--port", "8123"]) == 0
    assert captured["application"] == "alphaforge_api_server:app"
    assert captured["kwargs"] == {
        "host": "127.0.0.1",
        "port": 8123,
        "log_level": "info",
    }


def test_product_server_exposes_only_v3_research_controls():
    paths = {str(route.path) for route in server._iter_routes(server.app.routes)}
    lowered = {path.lower() for path in paths}

    assert "/api/veriquant/research-os/v3/plans" in paths
    assert "/api/veriquant/research-os/v3/runs" in paths
    assert not any("/research-os/v2" in path for path in lowered)
    assert not any(
        token in path
        for path in lowered
        for token in ("/live", "broker", "order", "oauth", "mandate", "account")
    )


def test_product_server_imports_without_pythonpath_bootstrap():
    agent_root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", "import alphaforge_api_server; print(alphaforge_api_server.app.title)"],
        cwd=agent_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "AlphaForge" in result.stdout
