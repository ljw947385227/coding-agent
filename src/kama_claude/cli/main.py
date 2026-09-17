from __future__ import annotations

import argparse
import sys

from kama_claude.cli.commands.chat import cmd_chat, cmd_resume
from kama_claude.cli.commands.core import cmd_core_start, cmd_core_status, cmd_core_stop
from kama_claude.cli.commands.eval import cmd_eval
from kama_claude.cli.commands.ping import cmd_ping
from kama_claude.cli.commands.run import cmd_run
from kama_claude.cli.commands.trace import cmd_trace
from kama_claude.cli.commands.version import cmd_version
from kama_claude.core.config import get_config
from kama_claude.core.logging_setup import setup_logging


def _repeat_count(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return parsed


# CLI 主入口：解析命令行参数并分发到对应子命令
def main() -> None:
    parser = argparse.ArgumentParser(prog="kama", description="KamaClaude CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ping", help="Ping the core daemon")
    subparsers.add_parser("chat", help="Start a multi-turn chat session")
    resume_parser = subparsers.add_parser(
        "resume", help="Choose and resume a session from the current workspace"
    )
    resume_parser.add_argument(
        "session_id",
        nargs="?",
        help="Advanced: resume an exact Session ID instead of choosing from the list",
    )

    run_parser = subparsers.add_parser("run", help="Run an agent task")
    run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")

    eval_parser = subparsers.add_parser(
        "eval", help="Run reproducible coding-agent tasks in isolated Git worktrees"
    )
    eval_parser.add_argument("task_file", help="JSON evaluation suite")
    eval_parser.add_argument("--output", help="Directory for evaluation artifacts")
    eval_parser.add_argument(
        "--repeats",
        type=_repeat_count,
        default=1,
        metavar="N",
        help="Repeat each task 1-100 times (default: 1)",
    )
    eval_parser.add_argument(
        "--keep-worktrees",
        action="store_true",
        help="Keep isolated worktrees after evaluation for debugging",
    )

    core_parser = subparsers.add_parser("core", help="Manage the core daemon")
    core_sub = core_parser.add_subparsers(dest="core_command")
    core_sub.add_parser("start", help="Start the daemon in the background")
    core_sub.add_parser("stop", help="Stop the running daemon")
    core_sub.add_parser("status", help="Show daemon status")

    trace_parser = subparsers.add_parser("trace", help="View system trace log")
    trace_parser.add_argument("run_id", nargs="?", default=None, help="Filter by run ID")
    trace_parser.add_argument("--layer", choices=["ipc", "event", "llm"], help="Filter by layer")
    trace_parser.add_argument("--direction", help="Filter by direction (e.g. CORE→LLM)")
    trace_parser.add_argument("--raw", action="store_true", help="Output raw NDJSON")
    trace_parser.add_argument("--follow", "-f", action="store_true", help="Follow new records")

    args = parser.parse_args()

    if args.version:
        cmd_version()
        return

    config = get_config()
    setup_logging(config)

    if args.command == "ping":
        cmd_ping(config)
    elif args.command == "chat":
        cmd_chat(config)
    elif args.command == "run":
        cmd_run(args.goal, config)
    elif args.command == "eval":
        cmd_eval(
            args.task_file,
            config,
            output=args.output,
            repeats=args.repeats,
            keep_worktrees=args.keep_worktrees,
        )
    elif args.command == "resume":
        cmd_resume(config, args.session_id)
    elif args.command == "core":
        if args.core_command == "start":
            cmd_core_start(config)
        elif args.core_command == "stop":
            cmd_core_stop(config)
        elif args.core_command == "status":
            cmd_core_status(config)
        else:
            core_parser.print_help()
            sys.exit(1)
    elif args.command == "trace":
        cmd_trace(
            args.run_id,
            config,
            layer=args.layer,
            direction=args.direction,
            raw=args.raw,
            follow=args.follow,
        )
    else:
        parser.print_help()
        sys.exit(1)
