from dataclasses import replace

from .enums import ActorStatus
from .errors import InvalidRequest, StaleActorGeneration
from .models import Actor


def replace_model_binding(
    actor: Actor,
    new_model_binding: str,
    *,
    expected_generation: int,
) -> Actor:
    if actor.generation != expected_generation:
        raise StaleActorGeneration(
            f"expected generation {expected_generation}, current generation {actor.generation}"
        )
    if actor.status is not ActorStatus.ACTIVE:
        raise InvalidRequest(f"Actor is not active: {actor.actor_id}")
    if not new_model_binding.strip():
        raise InvalidRequest("model binding must be non-empty")
    if new_model_binding == actor.model_binding:
        raise InvalidRequest("replacement model binding must differ from current binding")
    return replace(
        actor,
        generation=actor.generation + 1,
        model_binding=new_model_binding,
    )
