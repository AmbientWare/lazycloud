from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from database.repositories.compute import (
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from pydantic import computed_field
from shared.contracts import ContractModel
from shared.errors import NotFoundError
from shared.supplier_costs import SupplierCostTerms, SupplierCpuUnit

from database import DatabaseClient


class SupplierCostSnapshot(ContractModel):
    estimated: Literal[True] = True
    cpu_millicores: int
    memory_mib: int
    storage_mib: int | None
    gpu: str | None
    gpu_count: int
    supplier_cpu_unit: SupplierCpuUnit
    supplier_cpu_count: int | None
    terms: SupplierCostTerms

    @computed_field
    @property
    def known_hourly_micros(self) -> int | None:
        return self.terms.known_hourly_cost_micros

    @computed_field
    @property
    def complete_hourly_micros(self) -> int | None:
        return self.terms.complete_hourly_cost_micros


class SupplierNodeCostInspection(ContractModel):
    id: str
    provider_instance_id: str | None
    status: str
    costs: SupplierCostSnapshot


class SupplierUnitCostInspection(ContractModel):
    """Prepared offer and frozen first-observation node estimates, not supplier invoices."""

    unit_id: str
    workspace_id: str
    provider: str
    region: str
    offer_id: str
    offer: SupplierCostSnapshot
    nodes: list[SupplierNodeCostInspection]


@dataclass(frozen=True, slots=True)
class SupplierCostInspectionService:
    database: DatabaseClient

    def inspect(self, *, workspace_id: str, unit_id: str) -> SupplierUnitCostInspection:
        with self.database.session() as session:
            unit = ComputeUnitRepository(session).get(unit_id)
            if unit is None or unit.workspace_id != workspace_id:
                raise NotFoundError(f"compute unit not found in workspace: {unit_id}")
            instances = ComputeProviderInstanceRepository(session).list_for_pool(unit_id)
        return SupplierUnitCostInspection(
            unit_id=unit.id,
            workspace_id=unit.workspace_id,
            provider=unit.provider_ref,
            region=unit.region,
            offer_id=unit.offer_id,
            offer=SupplierCostSnapshot(
                cpu_millicores=unit.worker_cpu_millicores,
                memory_mib=unit.worker_memory_mib,
                storage_mib=unit.offer_storage_mib,
                gpu=unit.worker_gpu_type or None,
                gpu_count=unit.worker_gpu_count,
                supplier_cpu_unit=unit.supplier_cpu_unit,
                supplier_cpu_count=unit.supplier_cpu_count,
                terms=unit.offer_cost_terms
                if unit.offer_cost_terms is not None
                else SupplierCostTerms(),
            ),
            nodes=[
                SupplierNodeCostInspection(
                    id=instance.id,
                    provider_instance_id=instance.instance_id,
                    status=instance.status,
                    costs=SupplierCostSnapshot(
                        cpu_millicores=instance.cpu_millicores,
                        memory_mib=instance.memory_mb,
                        storage_mib=instance.storage_mib,
                        gpu=instance.gpu,
                        gpu_count=instance.gpu_count,
                        supplier_cpu_unit=instance.supplier_cpu_unit,
                        supplier_cpu_count=instance.supplier_cpu_count,
                        terms=instance.cost_terms,
                    ),
                )
                for instance in instances
            ],
        )
