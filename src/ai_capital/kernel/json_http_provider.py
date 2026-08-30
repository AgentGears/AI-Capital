from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.request import Request, urlopen
from uuid import uuid4

from .frozen_json import freeze_json
from .models import (
    CapabilityRequest,
    ClaimProposal,
    CompletionProposal,
    InferenceRequest,
    ModelTurn,
    ReasoningProposal,
)
from .serialization import canonical_json, to_canonical_data


_ALLOWED_RESPONSE_FIELDS = frozenset({
    "reasoning_proposals",
    "capability_requests",
    "claim_proposals",
    "completion_proposal",
})


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate response key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite response number: {value}")


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    return value


def _require_array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value


def _require_string(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    return value


def _require_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    return value


class JsonHttpInferenceProvider:
    """Generic HTTP/JSON adapter terminating transport vocabulary at the provider boundary."""

    def __init__(
        self,
        endpoint: str,
        model_profile: str,
        *,
        timeout_seconds: float = 30.0,
    ):
        if not endpoint.strip() or not model_profile.strip():
            raise ValueError("endpoint and model profile must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        self._endpoint = endpoint
        self._model_profile = model_profile
        self._timeout_seconds = float(timeout_seconds)

    def effective_configuration(self) -> Mapping[str, object]:
        return {
            "adapter": "json-http",
            "endpoint": self._endpoint,
            "model_profile": self._model_profile,
            "timeout_seconds": self._timeout_seconds,
        }

    def generate(self, request: InferenceRequest) -> ModelTurn:
        payload = canonical_json({
            "attempt_id": request.attempt_id,
            "actor_id": request.actor_id,
            "actor_generation": request.actor_generation,
            "program_id": request.program_id,
            "program_revision": request.program_revision,
            "model_profile": self._model_profile,
            "context_receipt": to_canonical_data(request.context_receipt),
            "context": to_canonical_data(request.context),
        }).encode("utf-8")
        outbound = Request(
            self._endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(outbound, timeout=self._timeout_seconds) as response:
            raw = response.read().decode("utf-8")

        decoded = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        body = _require_object(decoded, "provider response")
        unknown = set(body) - _ALLOWED_RESPONSE_FIELDS
        if unknown:
            raise ValueError(f"unknown provider response fields: {sorted(unknown)}")

        reasoning = tuple(
            ReasoningProposal(_require_string(item, "reasoning proposal"))
            for item in _require_array(body.get("reasoning_proposals", []), "reasoning_proposals")
        )

        capability_requests: list[CapabilityRequest] = []
        for item in _require_array(body.get("capability_requests", []), "capability_requests"):
            proposal = _require_object(item, "capability request")
            if set(proposal) != {"capability_id", "arguments", "expected_binding_revision"}:
                raise ValueError("capability request has unexpected fields")
            arguments = freeze_json(_require_object(proposal["arguments"], "capability arguments"))
            capability_requests.append(CapabilityRequest(
                request_id=str(uuid4()),
                capability_id=_require_string(proposal["capability_id"], "capability_id"),
                arguments=arguments,
                expected_binding_revision=_require_integer(
                    proposal["expected_binding_revision"],
                    "expected_binding_revision",
                ),
            ))

        claim_proposals: list[ClaimProposal] = []
        for item in _require_array(body.get("claim_proposals", []), "claim_proposals"):
            proposal = _require_object(item, "claim proposal")
            if set(proposal) != {"statement", "evidence_refs"}:
                raise ValueError("claim proposal has unexpected fields")
            evidence_refs = tuple(
                _require_string(ref, "evidence reference")
                for ref in _require_array(proposal["evidence_refs"], "evidence_refs")
            )
            claim_proposals.append(ClaimProposal(
                statement=_require_string(proposal["statement"], "claim statement"),
                evidence_refs=evidence_refs,
            ))

        completion_value = body.get("completion_proposal")
        completion = None
        if completion_value is not None:
            completion_body = _require_object(completion_value, "completion proposal")
            if set(completion_body) != {"rationale"}:
                raise ValueError("completion proposal has unexpected fields")
            completion = CompletionProposal(
                rationale=_require_string(completion_body["rationale"], "completion rationale")
            )

        return ModelTurn(
            provenance_receipt=request.attempt_id,
            reasoning_proposals=reasoning,
            capability_requests=tuple(capability_requests),
            claim_proposals=tuple(claim_proposals),
            completion_proposal=completion,
        )
