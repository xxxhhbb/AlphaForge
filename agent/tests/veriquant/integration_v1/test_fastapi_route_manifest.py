from __future__ import annotations

from dataclasses import dataclass

import pytest

import veriquant_api_server


@dataclass(frozen=True)
class _Route:
    path: str
    methods: set[str]
    endpoint: object = None


class _App:
    def __init__(self, routes: list[object]) -> None:
        self.routes = routes


def test_manifest_ignores_empty_non_route_objects_but_requires_complete_route_set() -> None:
    routes = list(veriquant_api_server.app.routes)
    app = _App([object(), _Route(path="", methods={"GET"}), *routes])

    manifest = veriquant_api_server.build_route_manifest(app)

    assert {route.path for route in manifest.routes} == veriquant_api_server.EXPECTED_ROUTE_PATHS
    assert all(set(route.methods) == {"GET"} for route in manifest.routes)


def test_manifest_fails_closed_when_required_route_is_missing() -> None:
    routes = [
        route
        for route in veriquant_api_server.app.routes
        if getattr(route, "path", None) != "/healthz"
    ]

    with pytest.raises(RuntimeError, match="route table mismatch"):
        veriquant_api_server.build_route_manifest(_App(routes))


def test_manifest_fails_closed_when_unexpected_route_is_present() -> None:
    routes = [
        *veriquant_api_server.app.routes,
        _Route(path="/veriquant/workbench/extra", methods={"GET"}),
    ]

    with pytest.raises(RuntimeError, match="route table mismatch"):
        veriquant_api_server.build_route_manifest(_App(routes))
