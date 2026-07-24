"""BenchHandoff command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from benchhandoff.engine import resume_run, start_run, verify_run
from benchhandoff.errors import BenchHandoffError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchhandoff",
        description="Run sequential benchmark tasks and verify their file evidence.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    start = subcommands.add_parser("start", help="start a new run")
    start.add_argument("suite", help="path to suite.toml")
    start.add_argument(
        "--run-dir",
        required=True,
        help="new evidence directory; it must be outside the suite tree",
    )

    resume = subcommands.add_parser("resume", help="resume an incomplete run")
    resume.add_argument("run_dir", help="existing run evidence directory")

    verify = subcommands.add_parser("verify", help="verify a completed bundle")
    verify.add_argument("run_dir", help="completed run evidence directory")
    return parser


def _emit(value: dict[str, object], *, stream: object = sys.stdout) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=stream,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Execute one CLI command and return a stable process exit code."""

    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "start":
            result = start_run(arguments.suite, arguments.run_dir)
            _emit(result.as_dict())
            return 0 if result.status == "completed" else 20
        if arguments.command == "resume":
            result = resume_run(arguments.run_dir)
            _emit(result.as_dict())
            return 0 if result.status == "completed" else 20
        verification = verify_run(arguments.run_dir)
        _emit(verification)
        return 0
    except BenchHandoffError as exc:
        _emit(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "detail": str(exc),
            },
            stream=sys.stderr,
        )
        return exc.exit_code
    except OSError as exc:
        _emit(
            {
                "status": "error",
                "error_type": type(exc).__name__,
                "detail": str(exc),
            },
            stream=sys.stderr,
        )
        return 30
