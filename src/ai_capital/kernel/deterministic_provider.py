from __future__ import annotations

from collections.abc import Mapping

from .models import InferenceRequest, ModelTurn, ReasoningProposal


class DeterministicInferenceProvider:
    """Small provider-neutral cognition implementation for contract qualification."""

    def __init__(
        self,
        response_text: str,
        *,
        configuration: Mapping[str, object] | None = None,
    ):
        self._response_text = response_text
        self._configuration = dict(configuration or {"kind": "deterministic", "revision": 1})

    def effective_configuration(self) -> Mapping[str, object]:
        return dict(self._configuration)

    def generate(self, request: InferenceRequest) -> ModelTurn:
        return ModelTurn(
            provenance_receipt=request.attempt_id,
            reasoning_proposals=(ReasoningProposal(self._response_text),),
        )
