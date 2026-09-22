from __future__ import annotations

from dataclasses import dataclass

from database.repositories.compute import ComputeUnitRepository
from shared.compute_policy import ComputeUnitProviderState
from shared.errors import ConflictError

from compute.providers import ProviderUnitRequest
from database import DatabaseClient


@dataclass(frozen=True, slots=True)
class ProviderUnitStateService:
    database: DatabaseClient

    def load(self, request: ProviderUnitRequest) -> ComputeUnitProviderState:
        with self.database.session() as session:
            state = ComputeUnitRepository(session).provider_checkpoint(
                request.unit_id,
                workspace_id=request.workspace_id,
                provider_ref=request.provider_ref,
                generation=request.generation,
            )
            if state is None:
                raise ConflictError("provider operation lost its capacity generation")
            return state

    def save(
        self,
        request: ProviderUnitRequest,
        *,
        expected: ComputeUnitProviderState,
        state: ComputeUnitProviderState,
    ) -> None:
        if state.revision != expected.revision + 1:
            raise ConflictError("provider checkpoint must advance its revision exactly once")
        with self.database.session() as session:
            if not ComputeUnitRepository(session).checkpoint_provider_state(
                request.unit_id,
                workspace_id=request.workspace_id,
                provider_ref=request.provider_ref,
                generation=request.generation,
                expected=expected,
                state=state,
            ):
                raise ConflictError("provider operation lost its capacity generation or state")
