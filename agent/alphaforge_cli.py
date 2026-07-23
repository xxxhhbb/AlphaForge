"""Public AlphaForge command line entry point.

The wrapper keeps the public interface small while delegating every operation
to the existing deterministic demo, research product, and loopback-only API
services.  It does not duplicate research, Evidence, or authority logic.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Callable


AGENT_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = AGENT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from cli._version import __version__


SPECIALISTS = (
    "alternative_data",
    "expectations_events",
    "fundamental",
    "macro_regime",
    "market_structure",
    "meta_alpha",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alphaforge",
        description="AlphaForge（千因研究院）自主多智能体 Alpha 研究平台",
    )
    parser.add_argument("--version", action="version", version=f"AlphaForge {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="运行零配置、完全离线的可信研究闭环")
    demo.add_argument("--offline", action="store_true", default=True, help="保留显式离线语义")
    demo.add_argument("--seed", type=int, default=7)
    demo.add_argument("--output-dir", type=Path)

    research = commands.add_parser("research", help="从自然语言问题启动完整 Alpha 研究")
    research.add_argument("question", help="需要研究的 Alpha 问题")
    research.add_argument("--seed", type=int, default=7)
    research.add_argument("--timeout", type=float, default=900.0)
    research.add_argument("--depth", default="标准研究")
    research.add_argument("--interaction-style", default="标准式")
    research.add_argument("--root", type=Path)
    research.add_argument("--data-root", type=Path)
    research.add_argument("--dotenv", type=Path)
    research.add_argument("--label-horizon", type=int, default=20)
    research.add_argument("--max-sessions", type=int)
    research.add_argument("--max-symbols", type=int)
    research.add_argument("--smoke", action="store_true")
    research.add_argument("--specialist", action="append", choices=SPECIALISTS)
    research.add_argument("--allow-dynamic-tools", action="store_true")
    research.add_argument("--vibe-tool", action="append")
    research.add_argument("--print-report", action="store_true")
    research.add_argument("--json", action="store_true")
    research.add_argument("--reuse-existing", action="store_true")
    research.add_argument("--progress-interval", type=float, default=10.0)

    serve = commands.add_parser("serve", help="启动仅限本机访问的研究 API 与 Web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    commands.add_parser("version", help="显示版本")
    return parser


def _append_option(argv: list[str], name: str, value: object | None) -> None:
    if value is not None:
        argv.extend((name, str(value)))


def _research_argv(args: argparse.Namespace) -> list[str]:
    argv = ["--question", args.question, "--seed", str(args.seed)]
    _append_option(argv, "--timeout", args.timeout)
    _append_option(argv, "--depth", args.depth)
    _append_option(argv, "--interaction-style", args.interaction_style)
    _append_option(argv, "--root", args.root)
    _append_option(argv, "--data-root", args.data_root)
    _append_option(argv, "--dotenv", args.dotenv)
    _append_option(argv, "--label-horizon", args.label_horizon)
    _append_option(argv, "--max-sessions", args.max_sessions)
    _append_option(argv, "--max-symbols", args.max_symbols)
    _append_option(argv, "--progress-interval", args.progress_interval)
    for specialist in args.specialist or ():
        argv.extend(("--specialist", specialist))
    for tool_name in args.vibe_tool or ():
        argv.extend(("--vibe-tool", tool_name))
    for enabled, flag in (
        (args.smoke, "--smoke"),
        (args.allow_dynamic_tools, "--allow-dynamic-tools"),
        (args.print_report, "--print-report"),
        (args.json, "--json"),
        (args.reuse_existing, "--reuse-existing"),
    ):
        if enabled:
            argv.append(flag)
    return argv


def _run_demo(args: argparse.Namespace) -> int:
    from veriquant.demo import main as demo_main

    argv = ["--offline", "--seed", str(args.seed)]
    _append_option(argv, "--output-dir", args.output_dir)
    return demo_main(argv)


def _run_research(args: argparse.Namespace) -> int:
    from veriquant.research_os_v3.terminal import main as research_main

    return research_main(_research_argv(args))


def _run_server(args: argparse.Namespace) -> int:
    from alphaforge_api_server import main as server_main

    return server_main(["--host", args.host, "--port", str(args.port)])


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "demo": _run_demo,
        "research": _run_research,
        "serve": _run_server,
    }
    if args.command == "version":
        print(f"AlphaForge {__version__}")
        return 0
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
