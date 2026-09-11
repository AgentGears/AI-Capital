from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .kernel.errors import AICapitalError
from .product.program_operator import LocalProgramOperator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-capital")
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
        help="Path to the local AI Capital database.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="Create a durable Program.")
    create.add_argument("--program-id", required=True)
    create.add_argument("--objective", required=True)
    create.add_argument("--constraint", action="append", default=[])
    create.add_argument("--success-criterion", action="append", default=[])

    commands.add_parser("list", help="List durable Programs.")

    show = commands.add_parser("show", help="Inspect a durable Program.")
    show.add_argument("program_id")

    start = commands.add_parser("start", help="Activate a created Program.")
    start.add_argument("program_id")
    start.add_argument("--expected-revision", required=True, type=int)

    pause = commands.add_parser("pause", help="Pause an active Program for the user.")
    pause.add_argument("program_id")
    pause.add_argument("--expected-revision", required=True, type=int)
    pause.add_argument("--expected-control-revision", required=True, type=int)

    resume = commands.add_parser("resume", help="Resume a user-paused active Program.")
    resume.add_argument("program_id")
    resume.add_argument("--expected-revision", required=True, type=int)
    resume.add_argument("--expected-control-revision", required=True, type=int)

    cancel = commands.add_parser("cancel", help="Cancel a non-terminal Program.")
    cancel.add_argument("program_id")
    cancel.add_argument("--expected-revision", required=True, type=int)

    return parser


def _write_json(stream: TextIO, value: Any) -> None:
    stream.write(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    stream.write("\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = _parser().parse_args(argv)

    try:
        with LocalProgramOperator.open(args.database) as operator:
            if args.command == "create":
                result = operator.create(
                    program_id=args.program_id,
                    objective=args.objective,
                    constraints=tuple(args.constraint),
                    success_criteria=tuple(args.success_criterion),
                )
            elif args.command == "list":
                result = operator.list()
            elif args.command == "show":
                result = operator.show(args.program_id)
            elif args.command == "start":
                result = operator.start(
                    args.program_id,
                    expected_revision=args.expected_revision,
                )
            elif args.command == "pause":
                result = operator.pause(
                    args.program_id,
                    expected_revision=args.expected_revision,
                    expected_control_revision=args.expected_control_revision,
                )
            elif args.command == "resume":
                result = operator.resume(
                    args.program_id,
                    expected_revision=args.expected_revision,
                    expected_control_revision=args.expected_control_revision,
                )
            elif args.command == "cancel":
                result = operator.cancel(
                    args.program_id,
                    expected_revision=args.expected_revision,
                )
            else:
                raise AssertionError(f"unhandled command: {args.command}")
    except AICapitalError as exc:
        _write_json(
            stderr,
            {
                "error": {
                    "code": type(exc).__name__,
                    "message": str(exc),
                }
            },
        )
        return 2

    _write_json(stdout, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
