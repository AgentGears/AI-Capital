from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from .actor_store import ActorRepository
from .durable_program import ProgramRepository
from .enums import ActorStatus, ModelAttemptOutcome
from .errors import (
    IntegrityViolation,
    InternalFault,
    InvalidRequest,
    StaleActorGeneration,
    StaleProgramRevision,
)
from .events import utc_now
from .frozen_json import FrozenMap, freeze_json
from .models import (
    ContextReceipt,
    InferenceRequest,
    ModelAttemptReceipt,
    ModelTurn,
)
from .serialization import canonical_digest


class InferenceProvider(Protocol):
    def effective_configuration(self) -> Mapping[str, object]: ...

    def generate(self, request: InferenceRequest) -> ModelTurn: ...


class ModelBindingRegistry:
    """Host-owned mapping from AI Capital binding IDs to inference adapters."""

    def __init__(self):
        self._bindings: dict[str, InferenceProvider] = {}

    def register(self, binding_id: str, provider: InferenceProvider) -> None:
        if not binding_id.strip():
            raise InvalidRequest("model binding ID must be non-empty")
        if binding_id in self._bindings:
            raise InvalidRequest(f"model binding already registered: {binding_id}")
        self._bindings[binding_id] = provider

    def resolve(self, binding_id: str) -> InferenceProvider:
        try:
            return self._bindings[binding_id]
        except KeyError as exc:
            raise InvalidRequest(f"unknown model binding: {binding_id}") from exc


@dataclass(frozen=True, slots=True)
class InferenceResult:
    receipt: ModelAttemptReceipt
    turn: ModelTurn


class InferenceHost:
    """Host boundary that turns replaceable model calls into receipted proposals."""

    def __init__(
        self,
        programs: ProgramRepository,
        actors: ActorRepository,
        bindings: ModelBindingRegistry,
    ):
        self._programs = programs
        self._actors = actors
        self._bindings = bindings

    def infer(
        self,
        *,
        program_id: str,
        actor_id: str,
        context_receipt: ContextReceipt,
        context: Mapping[str, object],
    ) -> InferenceResult:
        program = self._programs.get(program_id)
        actor = self._actors.get(actor_id)
        if actor.status is not ActorStatus.ACTIVE:
            raise InvalidRequest(f"Actor is not active: {actor_id}")
        if context_receipt.program_id != program.program_id:
            raise InvalidRequest("ContextReceipt references a different Program")
        if context_receipt.program_revision != program.revision:
            raise InvalidRequest("ContextReceipt is stale for current Program revision")

        provider = self._bindings.resolve(actor.model_binding)
        configuration = freeze_json(provider.effective_configuration())
        if not isinstance(configuration, FrozenMap):
            raise IntegrityViolation("effective model configuration must be an object")

        attempt_id = str(uuid4())
        request = InferenceRequest(
            attempt_id=attempt_id,
            actor_id=actor.actor_id,
            actor_generation=actor.generation,
            program_id=program.program_id,
            program_revision=program.revision,
            model_binding=actor.model_binding,
            context_receipt_ref=context_receipt.context_receipt_id,
            context=context,
        )
        input_digest = canonical_digest(request)
        started_at = utc_now()
        configuration_digest = canonical_digest(configuration)

        try:
            turn = provider.generate(request)
        except Exception as exc:
            finished_at = utc_now()
            receipt = ModelAttemptReceipt(
                attempt_id=attempt_id,
                actor_id=actor.actor_id,
                actor_generation=actor.generation,
                program_id=program.program_id,
                program_revision=program.revision,
                model_binding=actor.model_binding,
                context_receipt_ref=context_receipt.context_receipt_id,
                input_digest=input_digest,
                effective_config_digest=configuration_digest,
                outcome=ModelAttemptOutcome.FAILED,
                started_at=started_at,
                finished_at=finished_at,
                output_digest=None,
                error_code="provider_failure",
            )
            self._actors.record_attempt(receipt, None, request)
            raise InternalFault("inference provider failed") from exc

        finished_at = utc_now()
        if not isinstance(turn, ModelTurn) or turn.provenance_receipt != attempt_id:
            receipt = ModelAttemptReceipt(
                attempt_id=attempt_id,
                actor_id=actor.actor_id,
                actor_generation=actor.generation,
                program_id=program.program_id,
                program_revision=program.revision,
                model_binding=actor.model_binding,
                context_receipt_ref=context_receipt.context_receipt_id,
                input_digest=input_digest,
                effective_config_digest=configuration_digest,
                outcome=ModelAttemptOutcome.FAILED,
                started_at=started_at,
                finished_at=finished_at,
                output_digest=None,
                error_code="invalid_model_output",
            )
            self._actors.record_attempt(receipt, None, request)
            raise IntegrityViolation("inference provider returned invalid model output")

        output_digest = canonical_digest(turn)
        current_program = self._programs.get(program_id)
        current_actor = self._actors.get(actor_id)
        if current_program.revision != program.revision:
            receipt = ModelAttemptReceipt(
                attempt_id=attempt_id,
                actor_id=actor.actor_id,
                actor_generation=actor.generation,
                program_id=program.program_id,
                program_revision=program.revision,
                model_binding=actor.model_binding,
                context_receipt_ref=context_receipt.context_receipt_id,
                input_digest=input_digest,
                effective_config_digest=configuration_digest,
                outcome=ModelAttemptOutcome.STALE,
                started_at=started_at,
                finished_at=finished_at,
                output_digest=output_digest,
                error_code="stale_program_revision",
            )
            self._actors.record_attempt(receipt, turn, request)
            raise StaleProgramRevision("model output is stale for current Program revision")

        if (
            current_actor.generation != actor.generation
            or current_actor.model_binding != actor.model_binding
            or current_actor.status is not ActorStatus.ACTIVE
        ):
            receipt = ModelAttemptReceipt(
                attempt_id=attempt_id,
                actor_id=actor.actor_id,
                actor_generation=actor.generation,
                program_id=program.program_id,
                program_revision=program.revision,
                model_binding=actor.model_binding,
                context_receipt_ref=context_receipt.context_receipt_id,
                input_digest=input_digest,
                effective_config_digest=configuration_digest,
                outcome=ModelAttemptOutcome.STALE,
                started_at=started_at,
                finished_at=finished_at,
                output_digest=output_digest,
                error_code="stale_actor_generation",
            )
            self._actors.record_attempt(receipt, turn, request)
            raise StaleActorGeneration("model output is stale for current Actor generation")

        receipt = ModelAttemptReceipt(
            attempt_id=attempt_id,
            actor_id=actor.actor_id,
            actor_generation=actor.generation,
            program_id=program.program_id,
            program_revision=program.revision,
            model_binding=actor.model_binding,
            context_receipt_ref=context_receipt.context_receipt_id,
            input_digest=input_digest,
            effective_config_digest=configuration_digest,
            outcome=ModelAttemptOutcome.SUCCEEDED,
            started_at=started_at,
            finished_at=finished_at,
            output_digest=output_digest,
            error_code=None,
        )
        self._actors.record_attempt(receipt, turn, request)
        return InferenceResult(receipt=receipt, turn=turn)
