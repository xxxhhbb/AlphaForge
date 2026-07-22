from src.api.research_os_v2_routes import route_paths


def test_research_os_v2_route_surface_is_narrow():
    paths = set(route_paths())
    assert paths == {
        "/api/veriquant/research-os/v2/suggestions",
        "/api/veriquant/research-os/v2/plans/confirm",
        "/api/veriquant/research-os/v2/runs",
        "/api/veriquant/research-os/v2/runs/{run_id}",
    }
    rendered = " ".join(paths).lower()
    assert all(token not in rendered for token in ("live", "broker", "order", "oauth", "mandate"))
