from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .kernel.errors import AICapitalError, InvalidRequest
from .product.actor_provider import LocalActorProviderOperator
from .product.program_operator import LocalProgramOperator
from .product.provider_operator import LocalProviderOperator


_PROVIDER_COMMANDS = {
    "provider-register",
    "provider-update",
    "providers",
    "provider-show",
    "provider-history",
}
_ACTOR_PROVIDER_COMMANDS = {"actor-provider", "actor-rebind"}


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

    asks = commands.add_parser("asks", help="List Program-scoped approval requests.")
    asks.add_argument("program_id")
    approve = commands.add_parser("approve", help="Approve one current ASK decision.")
    approve.add_argument("decision_id")

    audit_operation = commands.add_parser(
        "audit-operation", help="Inspect authenticated Operation provenance."
    )
    audit_operation.add_argument("operation_id")
    audit_evidence = commands.add_parser(
        "audit-evidence", help="Inspect authenticated Evidence provenance."
    )
    audit_evidence.add_argument("evidence_id")
    audit_verification = commands.add_parser(
        "audit-verification", help="Inspect authenticated Verification provenance."
    )
    audit_verification.add_argument("verification_id")

    provider_register = commands.add_parser(
        "provider-register", help="Register non-secret provider binding metadata."
    )
    provider_register.add_argument("--binding-id", required=True)
    provider_register.add_argument("--adapter", required=True)
    provider_register.add_argument("--model", required=True)
    provider_register.add_argument("--settings-json", default="{}")

    provider_update = commands.add_parser(
        "provider-update", help="Update one provider binding revision."
    )
    provider_update.add_argument("binding_id")
    provider_update.add_argument("--expected-revision", required=True, type=int)
    provider_update.add_argument("--adapter", required=True)
    provider_update.add_argument("--model", required=True)
    provider_update.add_argument("--settings-json", default="{}")

    commands.add_parser("providers", help="List configured provider bindings.")
    provider_show = commands.add_parser("provider-show", help="Show one provider binding.")
    provider_show.add_argument("binding_id")
    provider_history = commands.add_parser(
        "provider-history", help="Show immutable provider revision history."
    )
    provider_history.add_argument("binding_id")

    actor_provider = commands.add_parser(
        "actor-provider", help="Inspect an Actor's current provider binding."
    )
    actor_provider.add_argument("actor_id")
    actor_rebind = commands.add_parser(
        "actor-rebind", help="Replace an active Actor's configured binding."
    )
    actor_rebind.add_argument("actor_id")
    actor_rebind.add_argument("binding_id")
    actor_rebind.add_argument("--expected-generation", required=True, type=int)
    actor_rebind.add_argument("--expected-provider-revision", required=True, type=int)
    return parser


def _write_json(stream: TextIO, value: Any) -> None:
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    stream.write("\n")


def _settings(payload: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InvalidRequest("settings-json must contain valid JSON") from exc
    if type(value) is not dict:
        raise InvalidRequest("settings-json must contain a JSON object")
    return value


def _program_command(args: argparse.Namespace) -> Any:
    with LocalProgramOperator.open(args.database) as operator:
        if args.command == "create":
            return operator.create(
                program_id=args.program_id,
                objective=args.objective,
                constraints=tuple(args.constraint),
                success_criteria=tuple(args.success_criterion),
            )
        if args.command == "list":
            return operator.list()
        if args.command == "show":
            return operator.show(args.program_id)
        if args.command == "start":
            return operator.start(args.program_id, expected_revision=args.expected_revision)
        if args.command == "pause":
            return operator.pause(
                args.program_id,
                expected_revision=args.expected_revision,
                expected_control_revision=args.expected_control_revision,
            )
        if args.command == "resume":
            return operator.resume(
                args.program_id,
                expected_revision=args.expected_revision,
                expected_control_revision=args.expected_control_revision,
            )
        if args.command == "cancel":
            return operator.cancel(args.program_id, expected_revision=args.expected_revision)
        if args.command == "asks":
            return operator.asks(args.program_id)
        if args.command == "approve":
            return operator.approve(args.decision_id)
        if args.command == "audit-operation":
            return operator.audit_operation(args.operation_id)
        if args.command == "audit-evidence":
            return operator.audit_evidence(args.evidence_id)
        if args.command == "audit-verification":
            return operator.audit_verification(args.verification_id)
    raise AssertionError(f"unhandled command: {args.command}")


def _provider_command(args: argparse.Namespace) -> Any:
    with LocalProviderOperator.open(args.database) as operator:
        if args.command == "provider-register":
            return operator.register(
                binding_id=args.binding_id,
                adapter=args.adapter,
                model=args.model,
                settings=_settings(args.settings_json),
            )
        if args.command == "provider-update":
            return operator.update(
                args.binding_id,
                expected_revision=args.expected_revision,
                adapter=args.adapter,
                model=args.model,
                settings=_settings(args.settings_json),
            )
        if args.command == "providers":
            return operator.list()
        if args.command == "provider-show":
            return operator.show(args.binding_id)
        if args.command == "provider-history":
            return operator.history(args.binding_id)
    raise AssertionError(f"unhandled provider command: {args.command}")


def _actor_provider_command(args: argparse.Namespace) -> Any:
    with LocalActorProviderOperator.open(args.database) as operator:
        if args.command == "actor-provider":
            return operator.show(args.actor_id)
        if args.command == "actor-rebind":
            return operator.rebind(
                args.actor_id,
                args.binding_id,
                expected_generation=args.expected_generation,
                expected_provider_revision=args.expected_provider_revision,
            )
    raise AssertionError(f"unhandled Actor provider command: {args.command}")


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
        if args.command in _PROVIDER_COMMANDS:
            result = _provider_command(args)
        elif args.command in _ACTOR_PROVIDER_COMMANDS:
            result = _actor_provider_command(args)
        else:
            result = _program_command(args)
    except AICapitalError as exc:
        _write_json(stderr, {"error": {"code": type(exc).__name__, "message": str(exc)}})
        return 2
    _write_json(stdout, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
