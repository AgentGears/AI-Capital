from __future__ import annotations

from collections.abc import Mapping

from .actor_store import ActorRepository
from .capability_store import CapabilityRepository
from .context import ContextRepository
from .durable_program import ProgramRepository
from .errors import IntegrityViolation, InvalidRequest
from .inference import InferenceHost, InferenceResult, ModelBindingRegistry
from .models import CapabilitySnapshot, ContextReceipt
from .serialization import canonical_json, to_canonical_data


_CAPABILITY_CONTEXT_KEY = "capability_snapshot"


class BoundedInferenceHost(InferenceHost):
    """Inference boundary that accepts only durable, receipted Context compilations."""

    def __init__(
        self,
        programs: ProgramRepository,
        actors: ActorRepository,
        bindings: ModelBindingRegistry,
        contexts: ContextRepository,
        capabilities: CapabilityRepository | None = None,
    ):
        if contexts._host_store is not programs:
            raise InvalidRequest("bounded inference Context must share the Host store")
        if actors._host_store is not programs:
            raise InvalidRequest("bounded inference Actor repository must share the Host store")
        if capabilities is not None and capabilities._host_store is not programs:
            raise InvalidRequest(
                "bounded inference Capability repository must share the Host store"
            )
        super().__init__(programs, actors, bindings, capabilities)
        self._contexts = contexts

    def infer(
        self,
        *,
        program_id: str,
        actor_id: str,
        context_receipt: ContextReceipt,
        context: Mapping[str, object],
        capability_snapshot: CapabilitySnapshot | None = None,
    ) -> InferenceResult:
        compiled = self._contexts.validate(context_receipt, context)
        if compiled.receipt.program_id != program_id:
            raise InvalidRequest("compiled Context references a different Program")

        effective_input = dict(compiled.context)
        prebound_snapshot = effective_input.pop(_CAPABILITY_CONTEXT_KEY, None)
        if prebound_snapshot is None:
            if capability_snapshot is not None:
                raise InvalidRequest(
                    "Capability snapshot was supplied but is absent from compiled Context"
                )
        else:
            if capability_snapshot is None:
                raise InvalidRequest(
                    "compiled Context contains a Capability snapshot that was not supplied"
                )
            expected_snapshot = to_canonical_data(capability_snapshot)
            if canonical_json(prebound_snapshot) != canonical_json(expected_snapshot):
                raise IntegrityViolation(
                    "compiled Capability snapshot differs from supplied durable snapshot"
                )

        result = super().infer(
            program_id=program_id,
            actor_id=actor_id,
            context_receipt=context_receipt,
            context=effective_input,
            capability_snapshot=capability_snapshot,
        )
        request = self._actors.request(result.receipt.attempt_id)
        if request.context != compiled.context:
            raise IntegrityViolation(
                "durable inference request differs from receipted compiled Context"
            )
        return result
