from __future__ import annotations

from pathlib import Path

import alphaforge_cli


def test_research_command_preserves_public_contract() -> None:
    args = alphaforge_cli._parser().parse_args(
        [
            "research",
            "寻找未来20日低相关Alpha",
            "--label-horizon",
            "20",
            "--specialist",
            "meta_alpha",
            "--print-report",
        ]
    )

    assert alphaforge_cli._research_argv(args) == [
        "--question",
        "寻找未来20日低相关Alpha",
        "--seed",
        "7",
        "--timeout",
        "900.0",
        "--depth",
        "标准研究",
        "--interaction-style",
        "标准式",
        "--label-horizon",
        "20",
        "--progress-interval",
        "10.0",
        "--specialist",
        "meta_alpha",
        "--print-report",
    ]


def test_demo_command_is_always_offline(monkeypatch, tmp_path: Path) -> None:
    captured: list[str] = []

    def fake_demo(argv: list[str]) -> int:
        captured.extend(argv)
        return 0

    monkeypatch.setattr("veriquant.demo.main", fake_demo)
    assert alphaforge_cli.main(["demo", "--seed", "11", "--output-dir", str(tmp_path)]) == 0
    assert captured == ["--offline", "--seed", "11", "--output-dir", str(tmp_path)]


def test_serve_rejects_non_loopback_before_starting_server() -> None:
    try:
        alphaforge_cli.main(["serve", "--host", "0.0.0.0"])
    except ValueError as exc:
        assert "loopback-only" in str(exc)
    else:  # pragma: no cover - fail loudly if the authority boundary regresses
        raise AssertionError("non-loopback AlphaForge server was accepted")


def test_container_bind_requires_explicit_opt_in(monkeypatch) -> None:
    import alphaforge_api_server

    monkeypatch.setenv("ALPHAFORGE_TRUST_CONTAINER_BIND", "1")
    assert alphaforge_api_server._validate_loopback_host("0.0.0.0") == "0.0.0.0"
