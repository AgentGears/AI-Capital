from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from .kernel.errors import AICapitalError, InvalidRequest
from .product.actor_provider import LocalActorProviderOperator
from .product.capability_operator import LocalCapabilityOperator
from .product.program_operator import LocalProgramOperator
from .product.provider_operator import LocalProviderOperator
from .product.workspace_operator import LocalWorkspaceOperator


_PROVIDER_COMMANDS = {
    "provider-register",
    "provider-update",
    "providers",
    "provider-show",
    "provider-history",
}
_ACTOR_PROVIDER_COMMANDS = {"actor-provider", "actor-rebind"}
_CAPABILITY_COMMANDS = {
    "capabilities",
    "capability-grant",
    "capability-grants",
    "capability-revoke",
    "capability-invoke",
    "capability-execute-approved",
}
_WORKSPACE_COMMANDS = {
    "snapshot",
    "snapshots",
    "snapshot-show",
    "artifacts",
    "artifact-read",
    "bundle-export",
    "bundle-import",
    "bundles",
    "bundle-show",
    "bundle-artifacts",
    "bundle-artifact-read",
}


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

    commands.add_parser(
        "capabilities", help="List the governed local product Capability profile."
    )
    capability_grant = commands.add_parser(
        "capability-grant", help="Issue an explicit Actor Capability Grant."
    )
    capability_grant.add_argument("actor_id")
    capability_grant.add_argument("capability_id")
    capability_grant.add_argument("--resource-scope", action="append", required=True)
    capability_grant.add_argument("--approval-required", action="store_true")
    capability_grant.add_argument("--expires-at")
    capability_grants = commands.add_parser(
        "capability-grants", help="List active Capability Grants for an Actor."
    )
    capability_grants.add_argument("actor_id")
    capability_revoke = commands.add_parser(
        "capability-revoke", help="Revoke one Capability Grant."
    )
    capability_revoke.add_argument("grant_id")
    capability_invoke = commands.add_parser(
        "capability-invoke",
        help="Resolve, authorize, and invoke one typed product Capability.",
    )
    capability_invoke.add_argument("program_id")
    capability_invoke.add_argument("actor_id")
    capability_invoke.add_argument("capability_id")
    capability_invoke.add_argument("--arguments-json", default="{}")
    capability_invoke.add_argument("--request-id")
    capability_execute = commands.add_parser(
        "capability-execute-approved",
        help="Execute one previously approved ASK decision through fresh authority.",
    )
    capability_execute.add_argument("decision_id")
    capability_execute.add_argument("approval_id")

    snapshot = commands.add_parser(
        "snapshot", help="Capture the local workspace for one exact Program revision."
    )
    snapshot.add_argument("program_id")
    snapshots = commands.add_parser(
        "snapshots", help="List authenticated workspace snapshots for a Program."
    )
    snapshots.add_argument("program_id")
    snapshot_show = commands.add_parser(
        "snapshot-show", help="Inspect one authenticated workspace snapshot."
    )
    snapshot_show.add_argument("snapshot_id")
    artifacts = commands.add_parser(
        "artifacts", help="List exact artifacts in one workspace snapshot."
    )
    artifacts.add_argument("snapshot_id")
    artifact_read = commands.add_parser(
        "artifact-read", help="Read one authenticated snapshot artifact as base64."
    )
    artifact_read.add_argument("snapshot_id")
    artifact_read.add_argument("path")

    bundle_export = commands.add_parser(
        "bundle-export", help="Export a deterministic Program bundle."
    )
    bundle_export.add_argument("program_id")
    bundle_export.add_argument("snapshot_id")
    bundle_export.add_argument("output", type=Path)
    bundle_import = commands.add_parser(
        "bundle-import", help="Import a Program bundle as a read-only archive."
    )
    bundle_import.add_argument("input", type=Path)
    commands.add_parser("bundles", help="List imported read-only Program bundles.")
    bundle_show = commands.add_parser(
        "bundle-show", help="Inspect one imported Program bundle."
    )
    bundle_show.add_argument("bundle_id")
    bundle_artifacts = commands.add_parser(
        "bundle-artifacts", help="List artifacts in one imported Program bundle."
    )
    bundle_artifacts.add_argument("bundle_id")
    bundle_artifact_read = commands.add_parser(
        "bundle-artifact-read", help="Read one imported bundle artifact as base64."
    )
    bundle_artifact_read.add_argument("bundle_id")
    bundle_artifact_read.add_argument("path")
    return parser


def _write_json(stream: TextIO, value: Any) -> None:
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    stream.write("\n")


def _json_object(payload: str, *, field: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InvalidRequest(f"{field} must contain valid JSON") from exc
    if type(value) is not dict:
        raise InvalidRequest(f"{field} must contain a JSON object")
    return value


def _settings(payload: str) -> dict[str, object]:
    return _json_object(payload, field="settings-json")


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


def _capability_command(args: argparse.Namespace) -> Any:
    with LocalCapabilityOperator.open(args.database) as operator:
        if args.command == "capabilities":
            return operator.capabilities()
        if args.command == "capability-grant":
            return operator.grant(
                actor_id=args.actor_id,
                capability_id=args.capability_id,
                resource_scope=tuple(args.resource_scope),
                approval_required=args.approval_required,
                expires_at=args.expires_at,
            )
        if args.command == "capability-grants":
            return operator.grants(args.actor_id)
        if args.command == "capability-revoke":
            return operator.revoke_grant(args.grant_id)
        if args.command == "capability-invoke":
            return operator.invoke(
                program_id=args.program_id,
                actor_id=args.actor_id,
                capability_id=args.capability_id,
                arguments=_json_object(args.arguments_json, field="arguments-json"),
                request_id=args.request_id,
            )
        if args.command == "capability-execute-approved":
            return operator.execute_approved(
                decision_id=args.decision_id,
                approval_id=args.approval_id,
            )
    raise AssertionError(f"unhandled capability command: {args.command}")


def _workspace_command(args: argparse.Namespace) -> Any:
    with LocalWorkspaceOperator.open(args.database) as operator:
        if args.command == "snapshot":
            return operator.snapshot(args.program_id)
        if args.command == "snapshots":
            return operator.snapshots(args.program_id)
        if args.command == "snapshot-show":
            return operator.show_snapshot(args.snapshot_id)
        if args.command == "artifacts":
            return operator.artifacts(args.snapshot_id)
        if args.command == "artifact-read":
            content = operator.artifact_bytes(args.snapshot_id, args.path)
            return {
                "path": args.path,
                "byte_length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        if args.command == "bundle-export":
            content = operator.export_bundle(args.program_id, args.snapshot_id)
            try:
                args.output.write_bytes(content)
            except OSError as exc:
                raise InvalidRequest(f"cannot write Program bundle: {args.output}") from exc
            return {
                "output": str(args.output),
                "byte_length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        if args.command == "bundle-import":
            try:
                content = args.input.read_bytes()
            except OSError as exc:
                raise InvalidRequest(f"cannot read Program bundle: {args.input}") from exc
            return operator.import_bundle(content)
        if args.command == "bundles":
            return operator.bundles()
        if args.command == "bundle-show":
            return operator.show_bundle(args.bundle_id)
        if args.command == "bundle-artifacts":
            return operator.bundle_artifacts(args.bundle_id)
        if args.command == "bundle-artifact-read":
            content = operator.bundle_artifact_bytes(args.bundle_id, args.path)
            return {
                "path": args.path,
                "byte_length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
    raise AssertionError(f"unhandled workspace command: {args.command}")


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
        elif args.command in _CAPABILITY_COMMANDS:
            result = _capability_command(args)
        elif args.command in _WORKSPACE_COMMANDS:
            result = _workspace_command(args)
        else:
            result = _program_command(args)
    except AICapitalError as exc:
        _write_json(stderr, {"error": {"code": type(exc).__name__, "message": str(exc)}})
        return 2
    _write_json(stdout, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
